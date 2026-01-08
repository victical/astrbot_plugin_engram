import os
import json
import datetime
from peewee import *
from playhouse.sqlite_ext import JSONField
from astrbot.api.star import StarTools

class BaseModel(Model):
    pass

class RawMemory(BaseModel):
    uuid = CharField(primary_key=True)
    session_id = CharField()
    user_id = CharField()
    user_name = CharField(null=True) # 新增：记录用户昵称
    role = CharField() # user 或 assistant
    content = TextField()
    msg_type = CharField()
    is_archived = BooleanField(default=False) # 新增：标记是否已归档总结
    timestamp = DateTimeField(default=datetime.datetime.now)

class MemoryIndex(BaseModel):
    index_id = CharField(primary_key=True) # 对应 ChromaDB 的 ID
    summary = TextField()
    ref_uuids = TextField() # 存储 JSON 字符串的 UUID 列表 (指向原文)
    prev_index_id = CharField(null=True) # 新增：链表结构，指向前一条总结，形成时间线
    source_type = CharField() # private 或 group
    user_id = CharField(null=True) # 记录所属用户
    active_score = IntegerField(default=100)
    created_at = DateTimeField(default=datetime.datetime.now)

class DatabaseManager:
    def __init__(self, data_dir):
        # 使用传入的规范插件数据目录
        self.data_dir = data_dir
        self.db_path = os.path.join(self.data_dir, "engram_memories.db")
        os.makedirs(self.data_dir, exist_ok=True)

        self.db = SqliteDatabase(self.db_path)
        
        # 将模型与数据库绑定
        RawMemory._meta.database = self.db
        MemoryIndex._meta.database = self.db
        
        self.init_db()

    def init_db(self):
        self.db.connect(reuse_if_open=True)
        self.db.create_tables([RawMemory, MemoryIndex])
        self.db.close()

    def save_raw_memory(self, **kwargs):
        with self.db.connection_context():
            return RawMemory.create(**kwargs)

    def get_unarchived_raw(self, session_id, limit=50):
        with self.db.connection_context():
            return list(RawMemory.select().where((RawMemory.session_id == session_id) & (RawMemory.is_archived == False)).order_by(RawMemory.timestamp.desc()).limit(limit))

    def mark_as_archived(self, uuids):
        with self.db.connection_context():
            RawMemory.update(is_archived=True).where(RawMemory.uuid << uuids).execute()

    def get_memories_by_uuids(self, uuids):
        with self.db.connection_context():
            return list(RawMemory.select().where(RawMemory.uuid << uuids).order_by(RawMemory.timestamp.asc()))

    def save_memory_index(self, **kwargs):
        with self.db.connection_context():
            return MemoryIndex.create(**kwargs)

    def get_last_memory_index(self, user_id):
        with self.db.connection_context():
            return MemoryIndex.select().where(MemoryIndex.user_id == user_id).order_by(MemoryIndex.created_at.desc()).first()

    def get_memory_index_by_id(self, index_id):
        with self.db.connection_context():
            return MemoryIndex.get_or_none(MemoryIndex.index_id == index_id)

    def get_memory_list(self, user_id, limit=5):
        with self.db.connection_context():
            return list(MemoryIndex.select().where(MemoryIndex.user_id == user_id).order_by(MemoryIndex.created_at.desc()).limit(limit))

    def get_memories_since(self, user_id, since_time):
        with self.db.connection_context():
            return list(MemoryIndex.select().where((MemoryIndex.user_id == user_id) & (MemoryIndex.created_at >= since_time)))

    def decay_active_scores(self, decay_rate=1):
        with self.db.connection_context():
            MemoryIndex.update(active_score=MemoryIndex.active_score - decay_rate).execute()

    def update_active_score(self, index_id, bonus=10):
        with self.db.connection_context():
            MemoryIndex.update(active_score=MemoryIndex.active_score + bonus).where(MemoryIndex.index_id == index_id).execute()

    def clear_user_data(self, user_id):
        """清除用户的所有记忆数据 (原始消息和总结索引)"""
        with self.db.connection_context():
            # 删除原始消息
            RawMemory.delete().where(RawMemory.user_id == user_id).execute()
            # 删除总结索引
            MemoryIndex.delete().where(MemoryIndex.user_id == user_id).execute()
