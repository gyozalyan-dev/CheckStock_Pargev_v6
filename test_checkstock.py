import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select

from app.database import Base
from app.models import Warehouse, Product, Stock, InTransitStock
from app import services


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        await services.seed_initial_data(session)
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_shortage_on_single_ff_with_network_hint(db: AsyncSession):
    """Проверка нехватки товара на выбранном ФФ при достаточном общем остатке по сети."""
    w_north = (await db.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()
    p_org = (await db.execute(select(Product).where(Product.sku == "SKU-101"))).scalar_one()

    with pytest.raises(services.BusinessError) as exc:
        await services.reserve_fbs(db, w_north.id, p_org.id, 12, "TEST_SHORTAGE_1")

    assert "Нехватка на складе «Север»" in str(exc.value)
    assert "общий доступный остаток по сети достаточен (16 шт." in str(exc.value)


@pytest.mark.asyncio
async def test_idempotent_transit_confirmation(db: AsyncSession):
    """Повторное подтверждение приёмки не должно начислять товар дважды."""
    w_north = (await db.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()
    w_south = (await db.execute(select(Warehouse).where(Warehouse.name == "Юг"))).scalar_one()
    p_org = (await db.execute(select(Product).where(Product.sku == "SKU-101"))).scalar_one()

    await services.transfer_between_ff(db, w_north.id, w_south.id, p_org.id, 3, "TEST_TRANS_1")
    transit = (await db.execute(select(InTransitStock))).scalar_one()

    await services.confirm_transit_delivery(db, transit.id, f"CONFIRM_{transit.id}")
    await services.confirm_transit_delivery(db, transit.id, f"CONFIRM_{transit.id}")
    await services.confirm_transit_delivery(db, transit.id, "CONFIRM_DUPLICATE_CALL")

    stock_south = (
        await db.execute(select(Stock).where(Stock.warehouse_id == w_south.id, Stock.product_id == p_org.id))
    ).scalar_one()
    assert stock_south.physical_qty == 9


@pytest.mark.asyncio
async def test_soft_delete_warehouse_preserves_stock_and_history(db: AsyncSession):
    """Мягкое удаление склада с остатком сохраняет данные и скрывает его из активного списка."""
    w_north = (await db.execute(select(Warehouse).where(Warehouse.name == "Север"))).scalar_one()
    await services.delete_warehouse(db, w_north.id, "TEST_DEL_NORTH")

    active_whs = (await db.execute(select(Warehouse).where(Warehouse.is_active == True))).scalars().all()
    assert all(w.name != "Север" for w in active_whs)

    stocks_north = (await db.execute(select(Stock).where(Stock.warehouse_id == w_north.id))).scalars().all()
    assert sum(s.physical_qty for s in stocks_north) == 15
