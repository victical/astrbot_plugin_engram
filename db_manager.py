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
    session_id = CharField(index=True)  # 添加索引：按会话查询
    user_id = CharField(index=True)     # 添加索引：按用户查询
    user_name = CharField(null=True)
    role = CharField()
    content = TextField()
    msg_type = CharField()
    is_archived = BooleanField(default=False, index=True)  # 添加索引：按归档状态查询
    timestamp = DateTimeField(default=datetime.datetime.now, index=True)  # 添加索引：按时间排序

    class Meta:
        indexes = (
            # 复合索引：常用查询组合
            (('session_id', 'is_archived'), False),
            (('user_id', 'is_archived'), False),
        )

class MemoryIndex(BaseModel):
    index_id = CharField(primary_key=True)
    summary = TextField()
    ref_uuids = TextField()
    prev_index_id = CharField(null=True, index=True)  # 添加索引：链表查询
    source_type = CharField()
    user_id = CharField(null=True, index=True)  # 添加索引：按用户查询
    active_score = IntegerField(default=100)
    created_at = DateTimeField(default=datetime.datetime.now, index=True)  # 添加索引：按时间排序

    class Meta:
        indexes = (
            # 复合索引：用户+时间查询
            (('user_id', 'created_at'), False),
        )

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

    def get_unarchived_raw(self, session_id, limit=None):
        with self.db.connection_context():
            query = RawMemory.select().where((RawMemory.session_id == session_id) & (RawMemory.is_archived == False)).order_by(RawMemory.timestamp.desc())
            if limit:
                query = query.limit(limit)
            return list(query)

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

    def delete_memory_index(self, index_id):
        """删除单条总结记忆索引"""
        with self.db.connection_context():
            MemoryIndex.delete().where(MemoryIndex.index_id == index_id).execute()
    
    def delete_raw_memories_by_uuids(self, uuids):
        """删除指定 UUID 的原始消息"""
        with self.db.connection_context():
            RawMemory.delete().where(RawMemory.uuid << uuids).execute()
    
    def clear_user_data(self, user_id):
        """清除用户的所有记忆数据 (原始消息和总结索引)"""
        with self.db.connection_context():
            # 删除原始消息
            RawMemory.delete().where(RawMemory.user_id == user_id).execute()
            # 删除总结索引
            MemoryIndex.delete().where(MemoryIndex.user_id == user_id).execute()
    
    def get_all_raw_messages(self, user_id, start_date=None, end_date=None, limit=None):
        """获取用户的所有原始消息（支持时间范围过滤）"""
        with self.db.connection_context():
            query = RawMemory.select().where(RawMemory.user_id == user_id)
            
            # 时间范围过滤
            if start_date:
                query = query.where(RawMemory.timestamp >= start_date)
            if end_date:
                query = query.where(RawMemory.timestamp <= end_date)
            
            # 按时间升序排列
            query = query.order_by(RawMemory.timestamp.asc())
            
            if limit:
                query = query.limit(limit)
            
            return list(query)
    
    def get_message_stats(self, user_id):
        """获取用户的消息统计信息"""
        with self.db.connection_context():
            total = RawMemory.select().where(RawMemory.user_id == user_id).count()
            archived = RawMemory.select().where((RawMemory.user_id == user_id) & (RawMemory.is_archived == True)).count()
            user_msgs = RawMemory.select().where((RawMemory.user_id == user_id) & (RawMemory.role == "user")).count()
            assistant_msgs = RawMemory.select().where((RawMemory.user_id == user_id) & (RawMemory.role == "assistant")).count()
            
            return {
                "total": total,
                "archived": archived,
                "unarchived": total - archived,
                "user_messages": user_msgs,
                "assistant_messages": assistant_msgs
            }
    
    def get_all_users_messages(self, start_date=None, end_date=None, limit=None):
        """获取所有用户的原始消息"""
        with self.db.connection_context():
            query = RawMemory.select()
            
            if start_date:
                query = query.where(RawMemory.timestamp >= start_date)
            if end_date:
                query = query.where(RawMemory.timestamp <= end_date)
            
            query = query.order_by(RawMemory.timestamp.asc())
            
            if limit:
                query = query.limit(limit)
            
            return list(query)
    
    def get_all_users_stats(self):
        """获取所有用户的统计信息"""
        with self.db.connection_context():
            total = RawMemory.select().count()
            archived = RawMemory.select().where(RawMemory.is_archived == True).count()
            user_count = RawMemory.select(RawMemory.user_id).distinct().count()
            user_msgs = RawMemory.select().where(RawMemory.role == "user").count()
            assistant_msgs = RawMemory.select().where(RawMemory.role == "assistant").count()
            
            return {
                "user_count": user_count,
                "total": total,
                "archived": archived,
                "unarchived": total - archived,
                "user_messages": user_msgs,
                "assistant_messages": assistant_msgs
            }
