import os
import json
import datetime
from peewee import *
from playhouse.sqlite_ext import JSONField, SqliteExtDatabase
from astrbot.api import logger
from astrbot.api.star import StarTools

class BaseModel(Model):
    class Meta:
        database = None

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
    @staticmethod
    def _bind_model(model_cls, database):
        """为指定数据库创建独立的模型类，避免跨 DB 互相污染。"""
        base_meta = getattr(model_cls, "Meta", object)
        meta = type(
            "Meta",
            (base_meta,),
            {
                "database": database,
                "table_name": model_cls._meta.table_name,
                "indexes": model_cls._meta.indexes,
            },
        )
        return type(
            f"{model_cls.__name__}Bound_{id(database)}",
            (model_cls,),
            {"Meta": meta},
        )

    def __init__(self, data_dir, db_path: str = None):
        # 使用传入的规范插件数据目录
        self.data_dir = data_dir
        self.db_path = db_path or os.path.join(self.data_dir, "engram_memories.db")
        os.makedirs(self.data_dir, exist_ok=True)

        try:
            abs_path = os.path.abspath(self.db_path)
            stat = os.stat(abs_path)
            logger.info(
                "Engram：DB 初始化路径=%s inode=%s size=%s",
                abs_path,
                getattr(stat, "st_ino", "-"),
                stat.st_size,
            )
        except Exception as e:
            logger.warning("Engram：DB 初始化路径解析失败：%s", e)

        self.db = SqliteExtDatabase(
            self.db_path,
            pragmas={
                "journal_mode": "wal",
                "cache_size": -64 * 1024,
                "synchronous": 1,
                "foreign_keys": 1
            }
        )

        # 为每个 DatabaseManager 生成独立模型，避免多 DB 互相覆盖
        self.RawMemory = self._bind_model(RawMemory, self.db)
        self.MemoryIndex = self._bind_model(MemoryIndex, self.db)

        self.init_db()

    def init_db(self):
        self.db.connect(reuse_if_open=True)
        self.db.create_tables([self.RawMemory, self.MemoryIndex])
        self.db.close()

    def save_raw_memory(self, **kwargs):
        with self.db.connection_context():
            return self.RawMemory.create(**kwargs)

    def get_unarchived_raw(self, session_id, limit=None):
        with self.db.connection_context():
            query = self.RawMemory.select().where((self.RawMemory.session_id == session_id) & (self.RawMemory.is_archived == False)).order_by(self.RawMemory.timestamp.desc())
            if limit:
                query = query.limit(limit)
            return list(query)

    def mark_as_archived(self, uuids):
        with self.db.connection_context():
            self.RawMemory.update(is_archived=True).where(self.RawMemory.uuid << uuids).execute()

    def get_memories_by_uuids(self, uuids):
        with self.db.connection_context():
            return list(self.RawMemory.select().where(self.RawMemory.uuid << uuids).order_by(self.RawMemory.timestamp.asc()))

    def save_memory_index(self, **kwargs):
        with self.db.connection_context():
            return self.MemoryIndex.create(**kwargs)

    def get_last_memory_index(self, user_id):
        with self.db.connection_context():
            return self.MemoryIndex.select().where(self.MemoryIndex.user_id == user_id).order_by(self.MemoryIndex.created_at.desc()).first()

    def get_memory_index_by_id(self, index_id):
        with self.db.connection_context():
            return self.MemoryIndex.get_or_none(self.MemoryIndex.index_id == index_id)

    def get_memory_indexes_by_ids(self, index_ids):
        """批量获取记忆索引，返回 {index_id: MemoryIndex} 映射"""
        if not index_ids:
            return {}
        with self.db.connection_context():
            query = self.MemoryIndex.select().where(self.MemoryIndex.index_id << index_ids)
            return {item.index_id: item for item in query}

    def get_prev_indices_by_ids(self, index_ids):
        """按 index_id 批量获取前序索引（兼容 MemoryManager 链路查询）。"""
        return self.get_memory_indexes_by_ids(index_ids)

    def get_raw_memories_map_by_uuid_lists(self, index_uuid_map):
        """批量按 UUID 列表获取原文，返回 {index_id: [RawMemory, ...]}。"""
        if not isinstance(index_uuid_map, dict) or not index_uuid_map:
            return {}

        all_uuids = []
        for uuids in index_uuid_map.values():
            if isinstance(uuids, (list, tuple, set)):
                all_uuids.extend([u for u in uuids if u])

        if not all_uuids:
            return {idx: [] for idx in index_uuid_map.keys()}

        # 去重后一次性查询，避免循环内多次 DB 往返
        unique_uuids = list(dict.fromkeys(all_uuids))
        with self.db.connection_context():
            query = self.RawMemory.select().where(self.RawMemory.uuid << unique_uuids)
            raw_by_uuid = {item.uuid: item for item in query}

        result_map = {}
        for idx, uuids in index_uuid_map.items():
            if not isinstance(uuids, (list, tuple, set)):
                result_map[idx] = []
                continue
            result_map[idx] = [raw_by_uuid[u] for u in uuids if u in raw_by_uuid]

        return result_map

    def get_memory_list(self, user_id, limit=5):
        with self.db.connection_context():
            return list(self.MemoryIndex.select().where(self.MemoryIndex.user_id == user_id).order_by(self.MemoryIndex.created_at.desc()).limit(limit))

    def search_memory_indexes_by_keywords(
        self,
        user_id,
        keywords,
        limit=50,
        start_time=None,
        end_time=None,
        source_types=None,
    ):
        """关键词兜底检索：在 SQLite 中按 summary 模糊匹配候选记忆。"""
        with self.db.connection_context():
            query = self.MemoryIndex.select().where(self.MemoryIndex.user_id == user_id)

            # 时间范围过滤（左闭右开）
            if start_time:
                query = query.where(self.MemoryIndex.created_at >= start_time)
            if end_time:
                query = query.where(self.MemoryIndex.created_at < end_time)

            # 来源类型过滤
            if isinstance(source_types, (list, tuple, set)):
                source_types = [str(t).strip() for t in source_types if str(t).strip()]
                if source_types:
                    query = query.where(self.MemoryIndex.source_type << source_types)

            # summary 关键词 OR 匹配
            normalized_keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
            if normalized_keywords:
                conditions = [self.MemoryIndex.summary.contains(k) for k in normalized_keywords]
                cond = conditions[0]
                for item in conditions[1:]:
                    cond = cond | item
                query = query.where(cond)

            try:
                limit = max(1, int(limit))
            except (TypeError, ValueError):
                limit = 50

            return list(query.order_by(self.MemoryIndex.created_at.desc()).limit(limit))

    def get_memories_since(self, user_id, since_time):
        with self.db.connection_context():
            return list(self.MemoryIndex.select().where((self.MemoryIndex.user_id == user_id) & (self.MemoryIndex.created_at >= since_time)))
    
    def get_memories_in_range(self, user_id, start_time, end_time):
        """获取指定时间范围内的记忆索引"""
        with self.db.connection_context():
            return list(self.MemoryIndex.select().where(
                (self.MemoryIndex.user_id == user_id) &
                (self.MemoryIndex.created_at >= start_time) &
                (self.MemoryIndex.created_at < end_time)
            ))

    def get_summaries_by_type(self, user_id, source_type, days=7):
        """按类型获取近 N 天总结，按时间倒序返回"""
        with self.db.connection_context():
            cutoff = datetime.datetime.now() - datetime.timedelta(days=max(1, int(days)))
            query = self.MemoryIndex.select().where(
                (self.MemoryIndex.user_id == user_id) &
                (self.MemoryIndex.source_type == source_type) &
                (self.MemoryIndex.created_at >= cutoff)
            ).order_by(self.MemoryIndex.created_at.desc())
            return list(query)

    def decay_active_scores(self, decay_rate=1):
        """全局衰减所有记忆的 active_score"""
        with self.db.connection_context():
            self.MemoryIndex.update(active_score=self.MemoryIndex.active_score - decay_rate).execute()

    def update_active_score(self, index_id, bonus=10):
        """给指定记忆加分（被召回时增强）"""
        with self.db.connection_context():
            self.MemoryIndex.update(active_score=self.MemoryIndex.active_score + bonus).where(self.MemoryIndex.index_id == index_id).execute()

    def get_cold_memory_ids(self, threshold=0):
        """获取 active_score <= threshold 的记忆 ID 列表（用于从 ChromaDB 修剪）"""
        with self.db.connection_context():
            return [m.index_id for m in self.MemoryIndex.select(self.MemoryIndex.index_id).where(self.MemoryIndex.active_score <= threshold)]

    def delete_memory_index(self, index_id):
        """删除单条总结记忆索引"""
        with self.db.connection_context():
            self.MemoryIndex.delete().where(self.MemoryIndex.index_id == index_id).execute()
    
    def delete_raw_memories_by_uuids(self, uuids):
        """删除指定 UUID 的原始消息"""
        with self.db.connection_context():
            self.RawMemory.delete().where(self.RawMemory.uuid << uuids).execute()
    
    def clear_user_data(self, user_id):
        """清除用户的所有记忆数据 (原始消息和总结索引)"""
        with self.db.connection_context():
            # 删除原始消息
            self.RawMemory.delete().where(self.RawMemory.user_id == user_id).execute()
            # 删除总结索引
            self.MemoryIndex.delete().where(self.MemoryIndex.user_id == user_id).execute()
    
    def get_all_raw_messages(self, user_id, start_date=None, end_date=None, limit=None):
        """获取用户的所有原始消息（支持时间范围过滤）"""
        with self.db.connection_context():
            query = self.RawMemory.select().where(self.RawMemory.user_id == user_id)
            
            # 时间范围过滤
            if start_date:
                query = query.where(self.RawMemory.timestamp >= start_date)
            if end_date:
                query = query.where(self.RawMemory.timestamp <= end_date)
            
            # 按时间升序排列
            query = query.order_by(self.RawMemory.timestamp.asc())
            
            if limit:
                query = query.limit(limit)
            
            return list(query)
    
    def get_message_stats(self, user_id):
        """获取用户的消息统计信息"""
        with self.db.connection_context():
            total = self.RawMemory.select().where(self.RawMemory.user_id == user_id).count()
            archived = self.RawMemory.select().where((self.RawMemory.user_id == user_id) & (self.RawMemory.is_archived == True)).count()
            user_msgs = self.RawMemory.select().where((self.RawMemory.user_id == user_id) & (self.RawMemory.role == "user")).count()
            assistant_msgs = self.RawMemory.select().where((self.RawMemory.user_id == user_id) & (self.RawMemory.role == "assistant")).count()
            
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
            query = self.RawMemory.select()
            
            if start_date:
                query = query.where(self.RawMemory.timestamp >= start_date)
            if end_date:
                query = query.where(self.RawMemory.timestamp <= end_date)
            
            query = query.order_by(self.RawMemory.timestamp.asc())
            
            if limit:
                query = query.limit(limit)
            
            return list(query)

    def get_all_user_ids(self):
        """获取所有出现过的用户ID"""
        with self.db.connection_context():
            return [row.user_id for row in self.RawMemory.select(self.RawMemory.user_id).distinct()]
    
    def get_all_users_stats(self):
        """获取所有用户的统计信息"""
        with self.db.connection_context():
            total = self.RawMemory.select().count()
            archived = self.RawMemory.select().where(self.RawMemory.is_archived == True).count()
            user_count = self.RawMemory.select(self.RawMemory.user_id).distinct().count()
            user_msgs = self.RawMemory.select().where(self.RawMemory.role == "user").count()
            assistant_msgs = self.RawMemory.select().where(self.RawMemory.role == "assistant").count()
            
            return {
                "user_count": user_count,
                "total": total,
                "archived": archived,
                "unarchived": total - archived,
                "user_messages": user_msgs,
                "assistant_messages": assistant_msgs
            }


class StableDatabaseInterface:
    """Engram DB 稳定接口层：统一收口并提供启动阶段契约自检。"""

    # 覆盖 MemoryManager.retrieve_memories 链路的最小稳定契约
    RETRIEVE_MEMORY_METHODS = (
        "get_memory_indexes_by_ids",
        "get_prev_indices_by_ids",
        "get_raw_memories_map_by_uuid_lists",
        "get_memories_by_uuids",
        "update_active_score",
    )

    # 覆盖当前插件主链路使用到的 DB 方法（启动阶段一次性自检）
    REQUIRED_METHODS = (
        "save_raw_memory",
        "get_unarchived_raw",
        "mark_as_archived",
        "get_memories_by_uuids",
        "save_memory_index",
        "get_last_memory_index",
        "get_memory_index_by_id",
        "get_memory_indexes_by_ids",
        "get_prev_indices_by_ids",
        "get_raw_memories_map_by_uuid_lists",
        "get_memory_list",
        "get_memories_since",
        "get_memories_in_range",
        "get_summaries_by_type",
        "decay_active_scores",
        "update_active_score",
        "get_cold_memory_ids",
        "delete_memory_index",
        "delete_raw_memories_by_uuids",
        "clear_user_data",
        "get_all_raw_messages",
        "get_message_stats",
        "get_all_users_messages",
        "get_all_user_ids",
        "get_all_users_stats",
    )

    def __init__(self, backend):
        self._backend = backend
        # 向后兼容：仍允许外部通过 self.db.db.connection_context() 使用底层连接
        self.db = getattr(backend, "db", None)

    def __getattr__(self, item):
        """默认代理到底层 DB 实现，避免破坏现有调用。"""
        return getattr(self._backend, item)

    def verify_contract(self, required_methods=None, stage="startup", raise_on_error=True):
        """验证底层 DB 是否满足约定接口。"""
        method_names = tuple(required_methods or self.REQUIRED_METHODS)
        missing = [name for name in method_names if not callable(getattr(self._backend, name, None))]

        if missing:
            missing_sorted = sorted(set(missing))
            message = (
                f"Engram DB 契约检查失败，阶段={stage}："
                f"缺失方法 -> {', '.join(missing_sorted)}"
            )
            logger.error(message)
            if raise_on_error:
                raise AttributeError(message)
            return False, missing_sorted

        logger.debug(
            "Engram DB 契约检查通过：阶段=%s（方法数=%d）",
            stage,
            len(method_names)
        )
        return True, []
