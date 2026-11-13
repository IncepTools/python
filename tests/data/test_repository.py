"""The ownership filter is not yours to remember."""

from __future__ import annotations

import pytest

from inceptools.core import ctxkit, errs
from inceptools.data import sqlx
from inceptools.data.repo import Repository
from inceptools.web import pagination

from .conftest import Product


def make_repo() -> Repository[Product]:
    return Repository(
        Product,
        entity_name="product",
        tenanted=True,
        sortable=("name", "sku"),
        default_sort="id",
    )


async def seed(repo: Repository[Product], session) -> None:
    for account, names in (("acc_1", "abc"), ("acc_2", "xy")):
        with ctxkit.use(account_id=account):
            for name in names:
                await repo.create(Product(name=name.upper(), sku=f"{account}-{name}"))
    await session.commit()


async def test_a_read_only_sees_its_own_account(sessions):
    repo = make_repo()
    async with sqlx.session_scope(sessions) as session:
        await seed(repo, session)
        with ctxkit.use(account_id="acc_1"):
            assert sorted(p.sku for p in await repo.find_all()) == ["acc_1-a", "acc_1-b", "acc_1-c"]
        with ctxkit.use(account_id="acc_2"):
            assert sorted(p.sku for p in await repo.find_all()) == ["acc_2-x", "acc_2-y"]


async def test_another_accounts_row_is_not_found_not_forbidden(sessions):
    """A 403 would confirm the id exists, which is itself a leak."""
    repo = make_repo()
    async with sqlx.session_scope(sessions) as session:
        await seed(repo, session)
        with ctxkit.use(account_id="acc_2"):
            assert await repo.find_by_id(1) is None
            with pytest.raises(errs.AppError) as caught:
                await repo.get_by_id(1)
            assert caught.value.status_code == 404


async def test_a_query_with_no_account_is_refused(sessions):
    """Never 'all accounts'. That is the failure that returns someone else's rows."""
    repo = make_repo()
    async with sqlx.session_scope(sessions):
        with pytest.raises(errs.AppError) as caught:
            await repo.find_all()
        assert caught.value.code == ctxkit.NO_TENANT
        assert caught.value.status_code == 401


async def test_create_takes_the_account_from_the_context_not_the_payload(sessions):
    repo = make_repo()
    async with sqlx.session_scope(sessions) as session:
        with ctxkit.use(account_id="acc_1"):
            created = await repo.create(Product(name="Spoofed", sku="s1", account_id="acc_2"))
            await session.commit()
            assert created.account_id == "acc_1"


async def test_unscoped_is_deliberate_and_visible(sessions):
    repo = make_repo()
    async with sqlx.session_scope(sessions) as session:
        await seed(repo, session)
        async with repo.unscoped():
            assert await repo.count() == 5


async def test_an_update_crosses_no_account_boundary(sessions):
    repo = make_repo()
    async with sqlx.session_scope(sessions) as session:
        await seed(repo, session)
        with ctxkit.use(account_id="acc_1"):
            assert await repo.update_by_id(4, {"name": "HACKED"}) == 0


async def test_a_mapping_update_can_set_a_boolean_to_false(sessions):
    """A model-based update cannot: it cannot tell 'leave it' from 'set false'."""
    repo = make_repo()
    async with sqlx.session_scope(sessions) as session:
        await seed(repo, session)
        with ctxkit.use(account_id="acc_1"):
            await repo.update_by_id(1, {"archived": True})
            await session.commit()
            assert (await repo.get_by_id(1)).archived is True
            await repo.update_by_id(1, {"archived": False})
            await session.commit()
            session.expire_all()
            assert (await repo.get_by_id(1)).archived is False


async def test_an_update_with_no_condition_is_refused(sessions):
    repo = make_repo()
    async with sqlx.session_scope(sessions):
        with ctxkit.use(account_id="acc_1"):
            with pytest.raises(errs.AppError, match="would rewrite every row"):
                await repo.update_fields({"name": "x"})
            with pytest.raises(errs.AppError, match="update with no values"):
                await repo.update_fields({}, id=1)


async def test_a_delete_with_no_condition_is_refused(sessions):
    repo = make_repo()
    async with sqlx.session_scope(sessions):
        with (
            ctxkit.use(account_id="acc_1"),
            pytest.raises(errs.AppError, match="would remove every row"),
        ):
            await repo.delete()


async def test_pagination_is_scoped_and_counted(sessions):
    repo = make_repo()
    async with sqlx.session_scope(sessions) as session:
        await seed(repo, session)
        with ctxkit.use(account_id="acc_1"):
            page = await repo.paginate(
                pagination.Query(page=1, limit=2, sort="name", order=pagination.Direction.DESC)
            )
            assert [p.name for p in page.items] == ["C", "B"]
            assert (page.total, page.pages) == (3, 2)


async def test_only_an_allow_listed_column_can_be_sorted(sessions):
    repo = make_repo()
    async with sqlx.session_scope(sessions):
        with ctxkit.use(account_id="acc_1"), pytest.raises(errs.AppError, match=r"sort"):
            await repo.paginate(pagination.Query(page=1, limit=10, sort="account_id"))


async def test_a_duplicate_key_becomes_a_409(sessions):
    repo = make_repo()
    async with sqlx.session_scope(sessions) as session:
        await seed(repo, session)
        with ctxkit.use(account_id="acc_1"), pytest.raises(errs.AppError) as caught:
            await repo.create(Product(name="dup", sku="acc_1-a"))
    assert caught.value.status_code == 409
    assert caught.value.code == "DUPLICATE"


async def test_a_driver_message_never_reaches_the_client(sessions):
    repo = make_repo()
    async with sqlx.session_scope(sessions) as session:
        await seed(repo, session)
        with ctxkit.use(account_id="acc_1"), pytest.raises(errs.AppError) as caught:
            await repo.create(Product(name="dup", sku="acc_1-a"))
    assert "IntegrityError" not in caught.value.client_message


def test_a_tenanted_repo_needs_the_column():
    with pytest.raises(errs.AppError, match="has no 'account_id_missing' column"):
        Repository(Product, tenanted=True, tenant_column="account_id_missing")
