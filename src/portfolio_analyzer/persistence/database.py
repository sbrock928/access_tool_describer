"""Small normalized SQLAlchemy schema, portable beyond SQLite."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import JSON, ForeignKey, String, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


class Base(DeclarativeBase):
    pass


class ApplicationRow(Base):
    __tablename__ = "applications"
    tool_inventory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(512))
    inventory_filename: Mapped[str] = mapped_column(String(1024))
    stated_description: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    source_path: Mapped[str] = mapped_column(String(4000))
    staged_path: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifacts: Mapped[list[ArtifactRow]] = relationship(back_populates="application")


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    tool_inventory_id: Mapped[str] = mapped_column(ForeignKey("applications.tool_inventory_id"))
    original_path: Mapped[str] = mapped_column(String(4000))
    local_path: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    application: Mapped[ApplicationRow] = relationship(back_populates="artifacts")


def create_session_factory(database_path: Path) -> sessionmaker[Session]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
