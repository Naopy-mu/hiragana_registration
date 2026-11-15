from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(timezone.utc)


def utc_iso(value):
    """SQLite drops timezone metadata; stored timestamps are always UTC."""
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()


class Base(DeclarativeBase):
    pass


class CommitUncertain(RuntimeError):
    """A lost commit acknowledgement must never trigger storage deletion."""


class Participant(Base):
    __tablename__ = 'participants'
    __table_args__ = {'sqlite_autoincrement': True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    registration_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    submission_id: Mapped[str] = mapped_column(String(36), unique=True)
    worksheet_revision_id: Mapped[str | None] = mapped_column(ForeignKey('study_revisions.id'), nullable=True)
    consent_version: Mapped[str] = mapped_column(String(100))
    consent_text: Mapped[str] = mapped_column(Text)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String(20), default='registered')
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def participant_id(self):
        return f'P{self.id:03d}'


class ExpiredRegistration(Base):
    __tablename__ = 'expired_registrations'
    registration_key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Page(Base):
    __tablename__ = 'pages'
    __table_args__ = (UniqueConstraint('submission_id', 'qr_code', name='uq_submission_qr'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey('participants.submission_id', ondelete='CASCADE'), index=True)
    qr_code: Mapped[str] = mapped_column(String(80))
    page_number: Mapped[int] = mapped_column(Integer)
    original_name: Mapped[str] = mapped_column(String(255))
    original_path: Mapped[str] = mapped_column(Text)
    corrected_path: Mapped[str] = mapped_column(Text)
    metrics_json: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StudyRevision(Base):
    __tablename__ = 'study_revisions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    manifest_json: Mapped[str] = mapped_column(Text)
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StudyState(Base):
    __tablename__ = 'study_state'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active_revision_id: Mapped[str] = mapped_column(ForeignKey('study_revisions.id'))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorksheetSubmission(Base):
    __tablename__ = 'worksheet_submissions'
    submission_id: Mapped[str] = mapped_column(ForeignKey('participants.submission_id', ondelete='CASCADE'), primary_key=True)
    worksheet_id: Mapped[str] = mapped_column(ForeignKey('study_revisions.id'), primary_key=True)
    pdf_path: Mapped[str] = mapped_column(Text)
    blank_qrs_json: Mapped[str] = mapped_column(Text, default='[]')
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Database:
    def __init__(self, settings):
        url = settings.database_url
        if settings.backend_mode == 'local':
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            url = f'sqlite:///{(settings.data_dir / "app.db").as_posix()}'
        elif url.startswith('postgresql://'):
            url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
        self.sqlite = url.startswith('sqlite:')
        self.engine = create_engine(url, pool_pre_ping=True, connect_args={'check_same_thread': False, 'timeout': 60} if self.sqlite else {})
        if self.sqlite:
            @event.listens_for(self.engine, 'connect')
            def setup_sqlite(conn, _):
                conn.execute('PRAGMA foreign_keys=ON')
                conn.execute('PRAGMA journal_mode=WAL')
            Base.metadata.create_all(self.engine)
            if 'worksheet_revision_id' not in {c['name'] for c in inspect(self.engine).get_columns('participants')}:
                with self.engine.begin() as connection:
                    connection.execute(text('ALTER TABLE participants ADD COLUMN worksheet_revision_id VARCHAR(36) REFERENCES study_revisions(id)'))
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def read(self):
        with self.sessions() as session:
            yield session

    @contextmanager
    def write(self):
        with self.sessions() as session:
            try:
                if self.sqlite:
                    from sqlalchemy import text
                    session.execute(text('BEGIN IMMEDIATE'))
                yield session
            except BaseException:
                session.rollback()
                raise
            else:
                try:
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    raise CommitUncertain('Database commit acknowledgement failed') from exc
