import asyncio
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import AsyncIterator

import uvicorn
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
templates_dir = os.path.join(CURRENT_DIR, "templates")
TEMPLATES_DIR = templates_dir if os.path.isdir(templates_dir) else CURRENT_DIR

DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Защищает операции с остатками от одновременного изменения
# внутри одного процесса приложения.
inventory_lock = asyncio.Lock()

FF_WAREHOUSE_IDS = {1, 2}
FBO_WAREHOUSE_IDS = {3, 4, 5}
FBS_WAREHOUSE_IDS = {7, 8, 9}
MARKETPLACE_WAREHOUSE_IDS = FBO_WAREHOUSE_IDS | FBS_WAREHOUSE_IDS

# ID FBS и FBO одного маркетплейса приводятся к общему ID
# только для отображения в таблице.
MARKETPLACE_DISPLAY_IDS = {
    3: 3,
    7: 3,
    4: 4,
    8: 4,
    5: 5,
    9: 5,
}

VALID_INCOME_SOURCE_IDS = {
    0,
    *FF_WAREHOUSE_IDS,
    *MARKETPLACE_WAREHOUSE_IDS,
}
VALID_TRANSFER_DESTINATION_IDS = FF_WAREHOUSE_IDS | MARKETPLACE_WAREHOUSE_IDS


class Warehouse(Base):
    __tablename__ = "warehouses"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)


class Stock(Base):
    __tablename__ = "stocks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False)
    source_ff_id = Column(Integer, nullable=False, default=0)
    warehouse_name = Column(String(100), nullable=False)
    sku = Column(String(100), nullable=False)
    name = Column(String(200), nullable=False)

    physical_qty = Column(Integer, nullable=False, default=0)
    reserved_qty = Column(Integer, nullable=False, default=0)
    transit_qty = Column(Integer, nullable=False, default=0)
    available_qty = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "warehouse_id",
            "source_ff_id",
            "sku",
            name="uq_stock_warehouse_source_sku",
        ),
        CheckConstraint(
            "physical_qty >= 0",
            name="ck_stock_physical_qty_nonnegative",
        ),
        CheckConstraint(
            "reserved_qty >= 0",
            name="ck_stock_reserved_qty_nonnegative",
        ),
        CheckConstraint(
            "transit_qty >= 0",
            name="ck_stock_transit_qty_nonnegative",
        ),
        CheckConstraint(
            "available_qty >= 0",
            name="ck_stock_available_qty_nonnegative",
        ),
    )


def normalize_sku(sku: str) -> str:
    return sku.strip().upper()


def alert_redirect(message: str | None = None) -> HTMLResponse:
    """Показывает сообщение и возвращает пользователя на главную страницу."""
    if message:
        safe_message = (
            json.dumps(message, ensure_ascii=True)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
        html = (
            "<!doctype html><html lang='ru'><meta charset='utf-8'>"
            f"<script>alert({safe_message});"
            "window.location.replace('/');</script></html>"
        )
    else:
        html = (
            "<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<script>window.location.replace('/');</script></html>"
        )

    return HTMLResponse(content=html)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_inventory_lock() -> AsyncIterator[None]:
    async with inventory_lock:
        yield


async def get_active_warehouse(
    db: AsyncSession,
    warehouse_id: int,
) -> Warehouse | None:
    result = await db.execute(
        select(Warehouse).where(
            Warehouse.id == warehouse_id,
            Warehouse.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def get_or_create_stock(
    db: AsyncSession,
    *,
    warehouse_id: int,
    source_ff_id: int,
    sku: str,
    name: str,
) -> Stock:
    result = await db.execute(
        select(Stock).where(
            Stock.warehouse_id == warehouse_id,
            Stock.source_ff_id == source_ff_id,
            Stock.sku == sku,
        )
    )
    stock = result.scalar_one_or_none()

    if stock is not None:
        return stock

    warehouse = await db.get(Warehouse, warehouse_id)
    warehouse_name = warehouse.name if warehouse else f"Склад {warehouse_id}"

    stock = Stock(
        warehouse_id=warehouse_id,
        source_ff_id=source_ff_id,
        warehouse_name=warehouse_name,
        sku=sku,
        name=name,
        physical_qty=0,
        reserved_qty=0,
        transit_qty=0,
        available_qty=0,
    )
    db.add(stock)
    return stock


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as session:
        warehouse_count = await session.scalar(
            select(func.count()).select_from(Warehouse)
        )

        # Начальные данные добавляются только в пустую базу.
        if not warehouse_count:
            session.add_all(
                [
                    Warehouse(id=1, name="Север"),
                    Warehouse(id=2, name="Юг"),
                    Warehouse(id=3, name="WB"),
                    Warehouse(id=4, name="Ozon"),
                    Warehouse(id=5, name="Яндекс.Маркет"),
                    Warehouse(id=7, name="WB"),
                    Warehouse(id=8, name="Ozon"),
                    Warehouse(id=9, name="Яндекс.Маркет"),
                ]
            )

            demo_stocks = [
                (1, "Север", "SKU-101", "Водка", 50),
                (1, "Север", "SKU-102", "Кола", 100),
                (1, "Север", "SKU-103", "Чипсы", 75),
                (1, "Север", "SKU-108", "sigar", 28),
                (2, "Юг", "SKU-101", "Водка", 30),
                (2, "Юг", "SKU-102", "Кола", 60),
                (2, "Юг", "SKU-103", "Чипсы", 40),
                (2, "Юг", "SKU-108", "sigar", 15),
            ]

            for warehouse_id, warehouse_name, sku, name, quantity in demo_stocks:
                session.add(
                    Stock(
                        warehouse_id=warehouse_id,
                        source_ff_id=0,
                        warehouse_name=warehouse_name,
                        sku=sku,
                        name=name,
                        physical_qty=quantity,
                        reserved_qty=0,
                        transit_qty=0,
                        available_qty=quantity,
                    )
                )

            await session.commit()

    yield
    await engine.dispose()


app = FastAPI(title="CheckStock", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    stock_result = await db.execute(select(Stock).order_by(Stock.id))
    stocks_list = stock_result.scalars().all()

    # Считаем FBS-резерв по исходному FF до группировки отображения.
    reserved_map: dict[tuple[int, str], int] = {}
    for stock in stocks_list:
        if stock.warehouse_id in FBS_WAREHOUSE_IDS:
            key = (stock.source_ff_id, stock.sku)
            reserved_map[key] = reserved_map.get(key, 0) + stock.reserved_qty

    for stock in stocks_list:
        if stock.warehouse_id in FF_WAREHOUSE_IDS:
            total_reserved = reserved_map.get(
                (stock.warehouse_id, stock.sku),
                0,
            )
            stock.available_qty = max(
                0,
                stock.physical_qty - total_reserved,
            )
        else:
            stock.available_qty = 0

    # Объединяем строки FBS/FBO одного маркетплейса и SKU
    # только для отображения. Записи в базе не меняются.
    display_stocks = []
    marketplace_rows = {}

    for stock in stocks_list:
        if stock.warehouse_id not in MARKETPLACE_WAREHOUSE_IDS:
            display_stocks.append(stock)
            continue

        display_warehouse_id = MARKETPLACE_DISPLAY_IDS[stock.warehouse_id]
        key = (display_warehouse_id, stock.sku)

        if key not in marketplace_rows:
            row = SimpleNamespace(
                id=stock.id,
                warehouse_id=display_warehouse_id,
                source_ff_id=0,
                warehouse_name=stock.warehouse_name,
                sku=stock.sku,
                name=stock.name,
                physical_qty=0,
                reserved_qty=0,
                transit_qty=0,
                available_qty=0,
            )
            marketplace_rows[key] = row
            display_stocks.append(row)

        row = marketplace_rows[key]

        if stock.warehouse_id in FBS_WAREHOUSE_IDS:
            row.reserved_qty += stock.reserved_qty
        else:
            row.transit_qty += stock.transit_qty

    display_stocks.sort(key=lambda stock: stock.id)

    warehouse_result = await db.execute(
        select(Warehouse).order_by(Warehouse.id)
    )
    warehouses_list = warehouse_result.scalars().all()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "stocks": display_stocks,
            "warehouses": warehouses_list,
        },
    )


@app.post("/stock/income")
async def income_endpoint(
    from_warehouse_id: int = Form(0),
    warehouse_id: int = Form(...),
    sku: str = Form(..., max_length=100),
    name: str = Form("Товар", max_length=200),
    quantity: int = Form(..., gt=0, le=1_000_000_000),
    _lock: None = Depends(get_inventory_lock),
    db: AsyncSession = Depends(get_db_session),
):
    sku = normalize_sku(sku)
    normalized_name = " ".join(name.strip().split())
    is_default_or_empty = (
        not normalized_name
        or normalized_name.casefold() == "товар"
    )

    if not sku:
        return alert_redirect("Укажите артикул (SKU) товара!")

    if warehouse_id not in FF_WAREHOUSE_IDS:
        return alert_redirect("Выберите корректный склад-получатель!")

    if from_warehouse_id not in VALID_INCOME_SOURCE_IDS:
        return alert_redirect("Выбран неизвестный склад-источник!")

    if from_warehouse_id == warehouse_id:
        return alert_redirect(
            "Склад-источник и склад-получатель совпадают!"
        )

    target_warehouse = await get_active_warehouse(db, warehouse_id)
    if target_warehouse is None:
        return alert_redirect(
            "Склад-получатель не найден или отключён!"
        )

    if from_warehouse_id != 0:
        source_warehouse = await get_active_warehouse(
            db,
            from_warehouse_id,
        )
        if source_warehouse is None:
            return alert_redirect(
                "Склад-источник не найден или отключён!"
            )

    # Сохраняем правило: один SKU соответствует одному названию товара.
    identity_result = await db.execute(
        select(Stock.sku, Stock.name).order_by(Stock.id)
    )
    identities = identity_result.all()

    existing_by_sku = next(
        (
            (existing_sku, existing_name)
            for existing_sku, existing_name in identities
            if existing_sku == sku
        ),
        None,
    )

    existing_by_name = None
    if not is_default_or_empty:
        existing_by_name = next(
            (
                (existing_sku, existing_name)
                for existing_sku, existing_name in identities
                if existing_name.casefold()
                == normalized_name.casefold()
            ),
            None,
        )

    if existing_by_name and existing_by_name[0] != sku:
        return alert_redirect(
            f"Товар «{existing_by_name[1]}» уже закреплён за артикулом "
            f"{existing_by_name[0]}. Пожалуйста, используйте этот артикул!"
        )

    if existing_by_sku:
        existing_name = existing_by_sku[1]

        if (
            not is_default_or_empty
            and existing_name.casefold()
            != normalized_name.casefold()
        ):
            return alert_redirect(
                f"Артикул {sku} уже закреплён за товаром "
                f"«{existing_name}». Укажите это наименование "
                "или оставьте поле названия пустым!"
            )

        clean_name = existing_name
    else:
        if is_default_or_empty:
            return alert_redirect(
                f"Артикул {sku} новый в системе — "
                "пожалуйста, укажите наименование товара!"
            )

        clean_name = normalized_name

    source_ff_stock = None
    marketplace_allocations: list[tuple[Stock, int]] = []

    if from_warehouse_id in FF_WAREHOUSE_IDS:
        # Перенос между FF: нельзя расходовать занятый FBS-резерв.
        source_result = await db.execute(
            select(Stock).where(
                Stock.warehouse_id == from_warehouse_id,
                Stock.source_ff_id == 0,
                Stock.sku == sku,
            )
        )
        source_ff_stock = source_result.scalar_one_or_none()

        reserved_result = await db.execute(
            select(func.coalesce(func.sum(Stock.reserved_qty), 0)).where(
                Stock.warehouse_id.in_(FBS_WAREHOUSE_IDS),
                Stock.source_ff_id == from_warehouse_id,
                Stock.sku == sku,
            )
        )
        reserved_qty = reserved_result.scalar_one()

        physical_qty = (
            source_ff_stock.physical_qty
            if source_ff_stock is not None
            else 0
        )
        available_qty = max(0, physical_qty - reserved_qty)

        if source_ff_stock is None or available_qty < quantity:
            return alert_redirect(
                "Недостаточно свободного товара на складе-источнике! "
                f"Доступно: {available_qty} шт.; "
                f"зарезервировано FBS: {reserved_qty} шт."
            )

    elif from_warehouse_id in MARKETPLACE_WAREHOUSE_IDS:
        # FBS-резерв и FBO-товар в пути выбираются строго
        # из соответствующего канала.
        is_fbs = from_warehouse_id in FBS_WAREHOUSE_IDS
        quantity_column = Stock.reserved_qty if is_fbs else Stock.transit_qty

        market_result = await db.execute(
            select(Stock)
            .where(
                Stock.warehouse_id == from_warehouse_id,
                Stock.sku == sku,
                quantity_column > 0,
            )
            .order_by(Stock.source_ff_id)
        )
        market_stocks = market_result.scalars().all()

        market_total = sum(
            stock.reserved_qty if is_fbs else stock.transit_qty
            for stock in market_stocks
        )

        if market_total < quantity:
            if is_fbs:
                return alert_redirect(
                    "Недостаточно товара в резерве FBS для возврата! "
                    f"В резерве: {market_total} шт."
                )
            return alert_redirect(
                "Недостаточно товара в пути FBO для возврата! "
                f"В пути: {market_total} шт."
            )

        # Списываем количество по строкам по порядку.
        remaining = quantity
        for market_stock in market_stocks:
            current_qty = (
                market_stock.reserved_qty
                if is_fbs
                else market_stock.transit_qty
            )
            take = min(current_qty, remaining)

            if take > 0:
                marketplace_allocations.append((market_stock, take))
                remaining -= take

            if remaining == 0:
                break

        if is_fbs:
            # При возврате FBS на другой FF проверяем исходный остаток.
            for market_stock, take in marketplace_allocations:
                origin_ff_id = market_stock.source_ff_id

                if origin_ff_id not in FF_WAREHOUSE_IDS:
                    return alert_redirect(
                        "У FBS-резерва не указан корректный исходный FF. "
                        "Проверьте складские остатки."
                    )

                if origin_ff_id == warehouse_id:
                    continue

                origin_result = await db.execute(
                    select(Stock).where(
                        Stock.warehouse_id == origin_ff_id,
                        Stock.source_ff_id == 0,
                        Stock.sku == sku,
                    )
                )
                origin_stock = origin_result.scalar_one_or_none()

                if origin_stock is None or origin_stock.physical_qty < take:
                    return alert_redirect(
                        "Недостаточно физического остатка на исходном FF "
                        "для переноса FBS-возврата. Проверьте остатки."
                    )

    target_stock = await get_or_create_stock(
        db,
        warehouse_id=warehouse_id,
        source_ff_id=0,
        sku=sku,
        name=clean_name,
    )

    if from_warehouse_id == 0:
        # Приход от поставщика.
        target_stock.physical_qty += quantity

    elif from_warehouse_id in FF_WAREHOUSE_IDS:
        # Перемещение между FF.
        source_ff_stock.physical_qty -= quantity
        target_stock.physical_qty += quantity

    elif from_warehouse_id in FBO_WAREHOUSE_IDS:
        # Возврат FBO: уменьшаем товар в пути
        # и приходуем его на выбранный FF.
        for market_stock, take in marketplace_allocations:
            market_stock.transit_qty -= take

        target_stock.physical_qty += quantity

    else:
        # Возврат FBS: снимаем резерв.
        # При выборе другого FF переносим туда физический остаток.
        for market_stock, take in marketplace_allocations:
            market_stock.reserved_qty -= take
            origin_ff_id = market_stock.source_ff_id

            if origin_ff_id != warehouse_id:
                origin_result = await db.execute(
                    select(Stock).where(
                        Stock.warehouse_id == origin_ff_id,
                        Stock.source_ff_id == 0,
                        Stock.sku == sku,
                    )
                )
                origin_stock = origin_result.scalar_one()

                origin_stock.physical_qty -= take
                target_stock.physical_qty += take

        # При возврате на исходный FF физический остаток не меняется.

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return alert_redirect(
            "Не удалось сохранить приход. "
            "Обновите страницу и повторите операцию."
        )

    return alert_redirect()


@app.post("/stock/transfer")
async def transfer_endpoint(
    from_warehouse_id: int = Form(...),
    to_warehouse_id: int = Form(...),
    sku: str = Form(..., max_length=100),
    quantity: int = Form(..., gt=0, le=1_000_000_000),
    _lock: None = Depends(get_inventory_lock),
    db: AsyncSession = Depends(get_db_session),
):
    sku = normalize_sku(sku)

    if not sku:
        return alert_redirect("Укажите артикул (SKU) товара!")

    if from_warehouse_id not in FF_WAREHOUSE_IDS:
        return alert_redirect("Выберите корректный склад-отправитель!")

    if to_warehouse_id not in VALID_TRANSFER_DESTINATION_IDS:
        return alert_redirect("Выберите корректный склад-получатель!")

    source_warehouse = await get_active_warehouse(
        db,
        from_warehouse_id,
    )
    if source_warehouse is None:
        return alert_redirect(
            "Склад-отправитель не найден или отключён!"
        )

    target_warehouse = await get_active_warehouse(db, to_warehouse_id)
    if target_warehouse is None:
        return alert_redirect(
            "Склад-получатель не найден или отключён!"
        )

    # Сначала проверяем, есть ли артикул на складе отправления.
    source_result = await db.execute(
        select(Stock).where(
            Stock.warehouse_id == from_warehouse_id,
            Stock.source_ff_id == 0,
            Stock.sku == sku,
        )
    )
    source_stock = source_result.scalar_one_or_none()

    if source_stock is None:
        return alert_redirect(
      "Такого товара с таким артикулом не существует!"
        )

    # Проверяем совпадение складов после проверки артикула.
    if from_warehouse_id == to_warehouse_id:
        return alert_redirect(
            "Склады отправления и назначения совпадают!"
        )

    # Учитываем все действующие FBS-резервы этого FF.
    reserved_result = await db.execute(
        select(
            func.coalesce(func.sum(Stock.reserved_qty), 0)
        ).where(
            Stock.warehouse_id.in_(FBS_WAREHOUSE_IDS),
            Stock.source_ff_id == from_warehouse_id,
            Stock.sku == sku,
        )
    )
    total_fbs_reserved = reserved_result.scalar_one()

    current_available = max(
        0,
        source_stock.physical_qty - total_fbs_reserved,
    )

    if current_available < quantity:
        return alert_redirect(
            "Недостаточно свободного товара! "
            f"Доступно: {current_available} шт.; "
            f"уже зарезервировано FBS: {total_fbs_reserved} шт."
        )

    if to_warehouse_id in FBO_WAREHOUSE_IDS:
        operation_type = "FBO"
        target_source_ff_id = from_warehouse_id
    elif to_warehouse_id in FBS_WAREHOUSE_IDS:
        operation_type = "FBS"
        target_source_ff_id = from_warehouse_id
    else:
        operation_type = "FF"
        target_source_ff_id = 0

    target_stock = await get_or_create_stock(
        db,
        warehouse_id=to_warehouse_id,
        source_ff_id=target_source_ff_id,
        sku=sku,
        name=source_stock.name,
    )

    if operation_type == "FF":
        # Перемещение между физическими складами FF.
        source_stock.physical_qty -= quantity
        target_stock.physical_qty += quantity

    elif operation_type == "FBO":
        # FBO: списываем физический остаток FF,
        # количество появляется в FBO-товаре в пути.
        source_stock.physical_qty -= quantity
        target_stock.transit_qty += quantity

    else:
        # FBS: товар остаётся физически на исходном FF,
        # но резервируется в отдельной строке FBS.
        target_stock.reserved_qty += quantity

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return alert_redirect(
            "Не удалось сохранить перенос. "
            "Обновите страницу и повторите операцию."
        )

    return alert_redirect()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=5051)