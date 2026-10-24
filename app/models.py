from sqlalchemy import Column, Integer, BigInteger, String, Text, DateTime, Date, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import relationship
from app.database import Base


class Admin(Base):
    """A choir leader (korleder). Owns choirs and songs; managed by the superadmin."""
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True)
    username = Column(String(255), nullable=False, unique=True)
    password = Column(String(255), nullable=False)  # scrypt hash, see app.auth
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login = Column(DateTime(timezone=True), nullable=True)
    quota_bytes = Column(BigInteger, nullable=True)  # NULL = the full QUOTA from .env; never above it

    choirs = relationship("Choir", back_populates="owner", cascade="all, delete-orphan", passive_deletes=True)
    songs = relationship("Song", back_populates="owner", cascade="all, delete-orphan", passive_deletes=True)


class Choir(Base):
    __tablename__ = "choirs"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    owner_id = Column(Integer, ForeignKey("admins.id", ondelete="CASCADE"), nullable=True)  # NULL only before migration
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_choirs_owner_name"),)

    owner = relationship("Admin", back_populates="choirs")
    users = relationship("User", back_populates="choir", cascade="all, delete-orphan")
    choir_songs = relationship("ChoirSong", back_populates="choir", cascade="all, delete-orphan")
    setlists = relationship("Setlist", back_populates="choir", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(255), nullable=False, unique=True)
    password = Column(String(255), nullable=False)  # scrypt hash, see app.auth
    choir_id = Column(Integer, ForeignKey("choirs.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login = Column(DateTime(timezone=True), nullable=True)

    choir = relationship("Choir", back_populates="users")


class Song(Base):
    __tablename__ = "songs"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    composer = Column(String(255), nullable=True)
    tags = Column(String(500), nullable=True)
    description = Column(Text, nullable=True)
    owner_id = Column(Integer, ForeignKey("admins.id", ondelete="CASCADE"), nullable=True)  # NULL only before migration
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("Admin", back_populates="songs")
    files = relationship("SongFile", back_populates="song", cascade="all, delete-orphan")
    choir_songs = relationship("ChoirSong", back_populates="song", cascade="all, delete-orphan")
    links = relationship("SongLink", back_populates="song", cascade="all, delete-orphan")


class SongFile(Base):
    __tablename__ = "song_files"

    id = Column(Integer, primary_key=True)
    song_id = Column(Integer, ForeignKey("songs.id", ondelete="CASCADE"), nullable=False)
    file_type = Column(String(10), nullable=False)  # 'pdf' or 'audio'
    part_name = Column(String(255), nullable=True)  # e.g. "Sopran", "Alt", "Tenor", "Bas"
    file_path = Column(String(500), nullable=False)
    original_filename = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    song = relationship("Song", back_populates="files")


class ChoirSong(Base):
    __tablename__ = "choir_songs"

    id = Column(Integer, primary_key=True)
    choir_id = Column(Integer, ForeignKey("choirs.id", ondelete="CASCADE"), nullable=False)
    song_id = Column(Integer, ForeignKey("songs.id", ondelete="CASCADE"), nullable=False)
    assigned_at = Column(DateTime(timezone=True), server_default=func.now())
    highlight_start = Column(Date, nullable=True)
    highlight_end = Column(Date, nullable=True)

    __table_args__ = (UniqueConstraint("choir_id", "song_id"),)

    choir = relationship("Choir", back_populates="choir_songs")
    song = relationship("Song", back_populates="choir_songs")


class Setlist(Base):
    __tablename__ = "setlists"

    id = Column(Integer, primary_key=True)
    choir_id = Column(Integer, ForeignKey("choirs.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    event_date = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    choir = relationship("Choir", back_populates="setlists")
    items = relationship("SetlistItem", back_populates="setlist", cascade="all, delete-orphan", order_by="SetlistItem.position")


class SetlistItem(Base):
    __tablename__ = "setlist_items"

    id = Column(Integer, primary_key=True)
    setlist_id = Column(Integer, ForeignKey("setlists.id", ondelete="CASCADE"), nullable=False)
    song_id = Column(Integer, ForeignKey("songs.id", ondelete="CASCADE"), nullable=True)  # None = placeholder
    placeholder_text = Column(String(255), nullable=True)
    position = Column(Integer, nullable=False, default=0)

    __table_args__ = (UniqueConstraint("setlist_id", "song_id"),)

    setlist = relationship("Setlist", back_populates="items")
    song = relationship("Song")


class SongLink(Base):
    __tablename__ = "song_links"

    id = Column(Integer, primary_key=True)
    song_id = Column(Integer, ForeignKey("songs.id", ondelete="CASCADE"), nullable=False)
    url = Column(String(2000), nullable=False)
    label = Column(String(255), nullable=True)

    song = relationship("Song", back_populates="links")
