from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models import Warehouse, Product, Stock, InTransitStock, OperationLog


class BusinessError(Exception):
    """Ошибка бизнес-логики с понятным объяснением для менеджера."""
    pass


async def seed_initial_data(db: AsyncSession) -> None:
    """Наполнение базы тестовыми данными из ТЗ при первом запуске."""
    res = await db.execute(select(func.count(Product.id)))
    if res.scalar_one() > 0:
        return

    w_north = Warehouse(name="Север", is_active=True)
    w_south = Warehouse(name="Юг", is_active=True)
    db.add_all([w_north, w_south])
    await db.flush()

    p1 = Product(sku="SKU-101", name="Органайзер")
    p2 = Product(sku="SKU-102", name="Контейнер")
    p3 = Product(sku="SKU-103", name="Корзина")
    db.add_all([p1, p2, p3])
    await db.flush()

    initial_stocks = [
        Stock(warehouse_id=w_north.id, product_id=p1.id, physical_qty=10, reserved_fbs_qty=0),
        Stock(warehouse_id=w_south.id, product_id=p1.id, physical_qty=6, reserved_fbs_qty=0),
        Stock(warehouse_id=w_north.id, product_id=p2.id, physical_qty=5, reserved_fbs_qty=0),
        Stock(warehouse_id=w_south.id, product_id=p2.id, physical_qty=0, reserved_fbs_qty=0),
        Stock(warehouse_id=w_north.id, product_id=p3.id, physical_qty=0, reserved_fbs_qty=0),
        Stock(warehouse_id=w_south.id, product_id=p3.id, physical_qty=4, reserved_fbs_qty=0),
    ]
    db.add_all(initial_stocks)

    db.add(
        OperationLog(
            idempotency_key="INIT_SEED_DATA",
            op_type="ПРИХОД (ТЕСТОВЫЕ ДАННЫЕ)",
            comment="Первичное наполнение остатков: Север и Юг по SKU-101, SKU-102, SKU-103",
        )
    )
    await db.commit()


async def _check_idempotency(db: AsyncSession, key: str) -> bool:
    """Возвращает True, если операция с таким ключом уже была выполнена."""
    if not key:
        return False
    res = await db.execute(select(OperationLog).where(OperationLog.idempotency_key == key))
    return res.scalar_one_or_none() is not None


async def _get_or_create_stock(db: AsyncSession, warehouse_id: int, product_id: int) -> Stock:
    res = await db.execute(
        select(Stock).where(Stock.warehouse_id == warehouse_id, Stock.product_id == product_id)
    )
    stock = res.scalar_one_or_none()
    if not stock:
        stock = Stock(warehouse_id=warehouse_id, product_id=product_id, physical_qty=0, reserved_fbs_qty=0)
        db.add(stock)
        await db.flush()
    return stock


async def _validate_available_qty(
    db: AsyncSession, warehouse: Warehouse, product: Product, needed_qty: int
) -> Stock:
    """Проверяет доступный остаток на выбранном ФФ и подсказывает про общий остаток сети."""
    if needed_qty <= 0:
        raise BusinessError("Количество должно быть больше нуля.")

    stock = await _get_or_create_stock(db, warehouse.id, product.id)
    if stock.available_qty >= needed_qty:
        return stock

    res = await db.execute(
        select(Stock)
        .options(selectinload(Stock.warehouse))
        .join(Warehouse)
        .where(Stock.product_id == product.id, Warehouse.is_active == True)
    )
    all_stocks = res.scalars().all()
    total_available = sum(s.available_qty for s in all_stocks)
    other_info = [
        f"{s.warehouse.name}: {s.available_qty} шт."
        for s in all_stocks
        if s.warehouse_id != warehouse.id and s.available_qty > 0
    ]

    if total_available >= needed_qty:
        others_str = ", ".join(other_info) if other_info else "других складах"
        raise BusinessError(
            f"Нехватка на складе «{warehouse.name}» для {product.sku} ({product.name}): "
            f"доступно {stock.available_qty} шт., требуется {needed_qty} шт. "
            f"При этом общий доступный остаток по сети достаточен ({total_available} шт. — есть на {others_str}). "
            f"Оформите перемещение между ФФ или выберите другой склад."
        )
    else:
        raise BusinessError(
            f"Недостаточно товара {product.sku} ({product.name}): на складе «{warehouse.name}» "
            f"доступно {stock.available_qty} шт., всего по сети доступно {total_available} шт. (требуется {needed_qty} шт.)."
        )


async def create_warehouse(db: AsyncSession, name: str, idempotency_key: str) -> None:
    if await _check_idempotency(db, idempotency_key):
        return
    clean_name = name.strip()
    if not clean_name:
        raise BusinessError("Название склада не может быть пустым.")
    w = Warehouse(name=clean_name, is_active=True)
    db.add(w)
    await db.flush()
    db.add(
        OperationLog(
            idempotency_key=idempotency_key,
            op_type="СОЗДАНИЕ СКЛАДА",
            to_warehouse_id=w.id,
            comment=f"Добавлен новый склад ФФ «{clean_name}»",
        )
    )
    await db.commit()


async def rename_warehouse(db: AsyncSession, warehouse_id: int, new_name: str, idempotency_key: str) -> None:
    if await _check_idempotency(db, idempotency_key):
        return
    clean_name = new_name.strip()
    if not clean_name:
        raise BusinessError("Новое название склада не может быть пустым.")
    w = await db.get(Warehouse, warehouse_id)
    if not w:
        raise BusinessError("Склад не найден.")
    old_name = w.name
    w.name = clean_name
    db.add(
        OperationLog(
            idempotency_key=idempotency_key,
            op_type="ПЕРЕИМЕНОВАНИЕ СКЛАДА",
            to_warehouse_id=w.id,
            comment=f"Склад «{old_name}» переименован в «{clean_name}»",
        )
    )
    await db.commit()


async def delete_warehouse(db: AsyncSession, warehouse_id: int, idempotency_key: str) -> None:
    """Мягкое удаление (архивация) склада с сохранением остатков и истории."""
    if await _check_idempotency(db, idempotency_key):
        return
    w = await db.get(Warehouse, warehouse_id)
    if not w:
        raise BusinessError("Склад не найден.")
    w.is_active = False
    db.add(
        OperationLog(
            idempotency_key=idempotency_key,
            op_type="УДАЛЕНИЕ (АРХИВАЦИЯ) СКЛАДА",
            from_warehouse_id=w.id,
            comment=f"Склад «{w.name}» удалён из активного списка. Остатки и история операций сохранены в архиве.",
        )
    )
    await db.commit()


async def inbound_stock(
    db: AsyncSession, warehouse_id: int, product_id: int, qty: int, idempotency_key: str
) -> None:
    if await _check_idempotency(db, idempotency_key):
        return
    if qty <= 0:
        raise BusinessError("Количество прихода должно быть больше нуля.")
    w = await db.get(Warehouse, warehouse_id)
    p = await db.get(Product, product_id)
    if not w or not w.is_active or not p:
        raise BusinessError("Выберите активный склад и корректный товар.")

    stock = await _get_or_create_stock(db, w.id, p.id)
    stock.physical_qty += qty

    db.add(
        OperationLog(
            idempotency_key=idempotency_key,
            op_type="ПРИХОД НА ФФ",
            product_id=p.id,
            to_warehouse_id=w.id,
            qty=qty,
            comment=f"Приход {qty} шт. ({p.sku} {p.name}) на склад «{w.name}»",
        )
    )
    await db.commit()


async def transfer_between_ff(
    db: AsyncSession, from_wh_id: int, to_wh_id: int, product_id: int, qty: int, idempotency_key: str
) -> None:
    if await _check_idempotency(db, idempotency_key):
        return
    if from_wh_id == to_wh_id:
        raise BusinessError("Склад-отправитель и склад-получатель должны различаться.")
    w_from = await db.get(Warehouse, from_wh_id)
    w_to = await db.get(Warehouse, to_wh_id)
    p = await db.get(Product, product_id)
    if not w_from or not w_from.is_active or not w_to or not w_to.is_active or not p:
        raise BusinessError("Выберите корректные активные склады и товар.")

    stock_from = await _validate_available_qty(db, w_from, p, qty)
    stock_from.physical_qty -= qty

    transit = InTransitStock(
        product_id=p.id,
        transit_type="FF_TRANSFER",
        from_warehouse_id=w_from.id,
        to_warehouse_id=w_to.id,
        qty=qty,
        status="IN_TRANSIT",
    )
    db.add(transit)
    db.add(
        OperationLog(
            idempotency_key=idempotency_key,
            op_type="ОТПРАВКА МЕЖДУ ФФ",
            product_id=p.id,
            from_warehouse_id=w_from.id,
            to_warehouse_id=w_to.id,
            qty=qty,
            comment=f"Отправлено в путь {qty} шт. ({p.sku} {p.name}) со склада «{w_from.name}» на «{w_to.name}»",
        )
    )
    await db.commit()


async def reserve_fbs(
    db: AsyncSession, warehouse_id: int, product_id: int, qty: int, idempotency_key: str
) -> None:
    if await _check_idempotency(db, idempotency_key):
        return
    w = await db.get(Warehouse, warehouse_id)
    p = await db.get(Product, product_id)
    if not w or not w.is_active or not p:
        raise BusinessError("Выберите активный склад и товар.")

    stock = await _validate_available_qty(db, w, p, qty)
    stock.reserved_fbs_qty += qty

    db.add(
        OperationLog(
            idempotency_key=idempotency_key,
            op_type="РЕЗЕРВ FBS",
            product_id=p.id,
            from_warehouse_id=w.id,
            qty=qty,
            comment=f"Зарезервировано под FBS {qty} шт. ({p.sku} {p.name}) на складе «{w.name}»",
        )
    )
    await db.commit()


async def cancel_reserve_fbs(
    db: AsyncSession, warehouse_id: int, product_id: int, qty: int, idempotency_key: str
) -> None:
    if await _check_idempotency(db, idempotency_key):
        return
    if qty <= 0:
        raise BusinessError("Количество отмены резерва должно быть больше нуля.")
    w = await db.get(Warehouse, warehouse_id)
    p = await db.get(Product, product_id)
    if not w or not p:
        raise BusinessError("Склад или товар не найден.")

    stock = await _get_or_create_stock(db, w.id, p.id)
    if stock.reserved_fbs_qty < qty:
        raise BusinessError(
            f"Для отправки по FBS необходимо сначала зарезервировать товар. "
            f"Сейчас в резерве на «{w.name}»: {stock.reserved_fbs_qty} шт., запрошено: {qty} шт."
        )
async def confirm_transit_delivery(db: AsyncSession, transit_id: int, idempotency_key: str) -> None:
    """Подтверждение приёмки товара в пути (идемпотентно — повторное нажатие не дублирует действие)."""
    if await _check_idempotency(db, idempotency_key):
        return
    res = await db.execute(
        select(InTransitStock)
        .options(
            selectinload(InTransitStock.product),
            selectinload(InTransitStock.from_warehouse),
            selectinload(InTransitStock.to_warehouse),
        )
        .where(InTransitStock.id == transit_id)
    )
    transit = res.scalar_one_or_none()
    if not transit:
        raise BusinessError("Запись о товаре в пути не найдена.")
    if transit.status != "IN_TRANSIT":
        return

    transit.status = "COMPLETED"
    p = transit.product

    if transit.transit_type == "FF_TRANSFER" and transit.to_warehouse_id:
        dest_stock = await _get_or_create_stock(db, transit.to_warehouse_id, p.id)
        dest_stock.physical_qty += transit.qty
        to_name = transit.to_warehouse.name if transit.to_warehouse else "ФФ"
        comment = f"Приёмка перемещения: {transit.qty} шт. ({p.sku} {p.name}) приняты на склад «{to_name}»"
        op_type = "ПРИЁМКА НА ФФ"
    elif transit.transit_type == "FBO_MP":
        comment = f"Приёмка FBO: {transit.qty} шт. ({p.sku} {p.name}) приняты складом маркетплейса"
        op_type = "ПРИЁМКА НА МП (FBO)"
    else:
        comment = f"Доставка FBS: {transit.qty} шт. ({p.sku} {p.name}) доставлены клиенту"
        op_type = "ДОСТАВЛЕНО КЛИЕНТУ (FBS)"

    db.add(
        OperationLog(
            idempotency_key=idempotency_key,
            op_type=op_type,
            product_id=p.id,
            from_warehouse_id=transit.from_warehouse_id,
            to_warehouse_id=transit.to_warehouse_id,
            qty=transit.qty,
            comment=comment,
        )
    )
    await db.commit()

