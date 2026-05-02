from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Module imports register tables on Base.metadata; must run after Base is defined.
from aslan_core.models import doc as _doc  # noqa: F401, E402
from aslan_core.models import streams as _streams  # noqa: F401, E402
from aslan_core.models import ts as _ts  # noqa: F401, E402
