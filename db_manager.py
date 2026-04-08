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
    group_id = CharField(null=True, index=True)
    member_id = CharField(null=True, index=True)
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


class DeleteHistory(BaseModel):
    id = AutoField()
    scope_key = CharField(index=True)  # 例如 private:<user_id> / group:<storage_id>
    user_id = CharField(null=True, index=True)
    group_id = CharField(null=True, index=True)
    source_type = CharField(default="private")

    index_id = CharField(index=True)
    summary = TextField()
    ref_uuids = TextField(null=True)
    prev_index_id = CharField(null=True)
    created_at = DateTimeField(null=True)
    active_score = IntegerField(default=100)
    delete_raw = BooleanField(default=False)
    deleted_uuids = TextField(null=True)
    vector_data = JSONField(null=True)

    is_restored = BooleanField(default=False, index=True)
    deleted_at = DateTimeField(default=datetime.datetime.now, index=True)
    restored_at = DateTimeField(null=True)

    class Meta:
        indexes = (
            (("scope_key", "is_restored", "deleted_at"), False),
        )


class PendingVectorJob(BaseModel):
    id = AutoField()
    index_id = CharField(index=True)
    user_id = CharField(null=True, index=True)
    source_type = CharField(default="private")
    summary = TextField()
    metadata = JSONField(null=True)
    retry_count = IntegerField(default=0)
    reason = TextField(null=True)
    queued_at = DateTimeField(default=datetime.datetime.now, index=True)

    class Meta:
        indexes = (
            (("index_id", "queued_at"), False),
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
        self.DeleteHistory = self._bind_model(DeleteHistory, self.db)
        self.PendingVectorJob = self._bind_model(PendingVectorJob, self.db)
        self._table_columns_cache = {}

        self.init_db()

    def init_db(self):
        self.db.connect(reuse_if_open=True)
        self.db.create_tables([
            self.RawMemory,
            self.MemoryIndex,
            self.DeleteHistory,
            self.PendingVectorJob,
        ])
        self._migrate_schema_if_needed()
        self._ensure_memory_index_fts()
        self.db.close()

    def _migrate_schema_if_needed(self):
        """自动迁移旧版 SQLite 表结构，避免升级后缺列报错。"""
        migration_plan = {
            self.RawMemory: {
                "group_id": "TEXT",
                "member_id": "TEXT",
            },
        }

        for model, columns in migration_plan.items():
            table_name = model._meta.table_name
            existing_columns = self._get_table_columns(model)

            for column_name, column_type in columns.items():
                if column_name in existing_columns:
                    continue
                try:
                    self.db.execute_sql(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                    )
                    logger.info(
                        "Engram：数据库自动迁移成功，表 %s 已新增字段 %s（%s）",
                        table_name,
                        column_name,
                        column_type,
                    )
                except Exception as e:
                    logger.error(
                        "Engram：数据库自动迁移失败，表 %s 新增字段 %s 时出错：%s",
                        table_name,
                        column_name,
                        e,
                    )
                    raise

            if model is self.RawMemory:
                self._ensure_index_exists(
                    table_name,
                    "rawmemory_group_id_idx",
                    "group_id",
                    existing_columns | set(columns.keys()),
                )
                self._ensure_index_exists(
                    table_name,
                    "rawmemory_member_id_idx",
                    "member_id",
                    existing_columns | set(columns.keys()),
                )

        self._table_columns_cache.clear()

    def _ensure_memory_index_fts(self):
        """为 MemoryIndex 构建 FTS5 索引与触发器，用于 BM25 候选召回。"""
        table_name = self.MemoryIndex._meta.table_name
        fts_table = f"{table_name}_fts"

        with self.db.connection_context():
            # 外部内容 FTS：summary 可检索，其他字段仅用于 bm25 参数占位/诊断
            self.db.execute_sql(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {fts_table}
                USING fts5(
                    summary,
                    user_id UNINDEXED,
                    source_type UNINDEXED,
                    created_at UNINDEXED,
                    content='{table_name}',
                    content_rowid='rowid',
                    tokenize='unicode61'
                )
                """
            )

            # 触发器：保持 FTS 与主表一致
            self.db.execute_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table_name}_ai
                AFTER INSERT ON {table_name}
                BEGIN
                    INSERT INTO {fts_table}(rowid, summary, user_id, source_type, created_at)
                    VALUES (new.rowid, new.summary, new.user_id, new.source_type, new.created_at);
                END;
                """
            )
            self.db.execute_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table_name}_ad
                AFTER DELETE ON {table_name}
                BEGIN
                    INSERT INTO {fts_table}({fts_table}, rowid, summary, user_id, source_type, created_at)
                    VALUES('delete', old.rowid, old.summary, old.user_id, old.source_type, old.created_at);
                END;
                """
            )
            self.db.execute_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table_name}_au
                AFTER UPDATE ON {table_name}
                BEGIN
                    INSERT INTO {fts_table}({fts_table}, rowid, summary, user_id, source_type, created_at)
                    VALUES('delete', old.rowid, old.summary, old.user_id, old.source_type, old.created_at);
                    INSERT INTO {fts_table}(rowid, summary, user_id, source_type, created_at)
                    VALUES (new.rowid, new.summary, new.user_id, new.source_type, new.created_at);
                END;
                """
            )

            # 首次/升级后重建索引，确保旧数据可检索（避免每次启动全量重建）
            try:
                fts_count = self.db.execute_sql(f"SELECT COUNT(1) FROM {fts_table}").fetchone()[0]
            except Exception:
                fts_count = 0
            if int(fts_count or 0) == 0:
                self.db.execute_sql(f"INSERT INTO {fts_table}({fts_table}) VALUES('rebuild')")


    def _ensure_index_exists(self, table_name: str, index_name: str, column_name: str, available_columns=None):
        available_columns = available_columns or set()
        if column_name not in available_columns:
            return
        try:
            self.db.execute_sql(
                f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name}({column_name})"
            )
            logger.debug(
                "Engram：数据库索引检查完成，表 %s 字段 %s 索引=%s",
                table_name,
                column_name,
                index_name,
            )
        except Exception as e:
            logger.warning(
                "Engram：创建索引失败，表 %s 字段 %s 索引=%s：%s",
                table_name,
                column_name,
                index_name,
                e,
            )

    def _get_table_columns(self, model):
        table_name = model._meta.table_name
        cached = self._table_columns_cache.get(table_name)
        if cached is not None:
            return cached
        with self.db.connection_context():
            rows = self.db.execute_sql(f"PRAGMA table_info({table_name})").fetchall()
        columns = {str(row[1]) for row in rows if len(row) > 1}
        self._table_columns_cache[table_name] = columns
        return columns

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
            available_columns = self._get_table_columns(self.RawMemory)
            fields = [
                self.RawMemory.uuid,
                self.RawMemory.session_id,
                self.RawMemory.user_id,
                self.RawMemory.user_name,
                self.RawMemory.role,
                self.RawMemory.content,
                self.RawMemory.msg_type,
                self.RawMemory.is_archived,
                self.RawMemory.timestamp,
            ]
            if "group_id" in available_columns and hasattr(self.RawMemory, "group_id"):
                fields.append(self.RawMemory.group_id)
            if "member_id" in available_columns and hasattr(self.RawMemory, "member_id"):
                fields.append(self.RawMemory.member_id)
            return list(
                self.RawMemory.select(*fields)
                .where(self.RawMemory.uuid << uuids)
                .order_by(self.RawMemory.timestamp.asc())
            )

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

    def _search_memory_indexes_by_keywords_like(
        self,
        user_id,
        normalized_keywords,
        limit=50,
        start_time=None,
        end_time=None,
        source_types=None,
    ):
        """旧版兜底：SQLite LIKE 候选检索。"""
        query = self.MemoryIndex.select().where(self.MemoryIndex.user_id == user_id)

        if start_time:
            query = query.where(self.MemoryIndex.created_at >= start_time)
        if end_time:
            query = query.where(self.MemoryIndex.created_at < end_time)

        if isinstance(source_types, (list, tuple, set)):
            source_types = [str(t).strip() for t in source_types if str(t).strip()]
            if source_types:
                query = query.where(self.MemoryIndex.source_type << source_types)

        if normalized_keywords:
            conditions = [self.MemoryIndex.summary.contains(k) for k in normalized_keywords]
            cond = conditions[0]
            for item in conditions[1:]:
                cond = cond | item
            query = query.where(cond)

        return list(query.order_by(self.MemoryIndex.created_at.desc()).limit(limit))

    def search_memory_indexes_by_keywords(
        self,
        user_id,
        keywords,
        limit=50,
        start_time=None,
        end_time=None,
        source_types=None,
        use_bm25=True,
    ):
        """关键词兜底检索：优先 FTS5 BM25，失败时回退 LIKE。"""
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 50

        normalized_keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]

        table_name = self.MemoryIndex._meta.table_name
        fts_table = f"{table_name}_fts"

        with self.db.connection_context():
            # 无关键词时沿用时间倒序
            if not normalized_keywords:
                return self._search_memory_indexes_by_keywords_like(
                    user_id=user_id,
                    normalized_keywords=normalized_keywords,
                    limit=limit,
                    start_time=start_time,
                    end_time=end_time,
                    source_types=source_types,
                )

            if not use_bm25:
                return self._search_memory_indexes_by_keywords_like(
                    user_id=user_id,
                    normalized_keywords=normalized_keywords,
                    limit=limit,
                    start_time=start_time,
                    end_time=end_time,
                    source_types=source_types,
                )

            # FTS MATCH 表达式：关键词 OR，短语精确匹配
            match_tokens = []
            for token in normalized_keywords[:24]:
                safe = token.replace('"', '""').strip()
                if safe:
                    match_tokens.append(f'"{safe}"')
            match_expr = " OR ".join(match_tokens)

            where_sql = ["mi.user_id = ?", f"{fts_table} MATCH ?"]
            params = [str(user_id), match_expr]

            if start_time:
                where_sql.append("mi.created_at >= ?")
                params.append(start_time)
            if end_time:
                where_sql.append("mi.created_at < ?")
                params.append(end_time)

            if isinstance(source_types, (list, tuple, set)):
                source_types = [str(t).strip() for t in source_types if str(t).strip()]
                if source_types:
                    placeholders = ",".join(["?"] * len(source_types))
                    where_sql.append(f"mi.source_type IN ({placeholders})")
                    params.extend(source_types)

            params.append(limit)

            sql = f"""
                SELECT mi.index_id, bm25({fts_table}) AS bm25_score
                FROM {fts_table}
                JOIN {table_name} AS mi ON mi.rowid = {fts_table}.rowid
                WHERE {' AND '.join(where_sql)}
                ORDER BY bm25_score ASC, mi.created_at DESC
                LIMIT ?
            """

            try:
                rows = self.db.execute_sql(sql, params).fetchall()
                ordered_ids = [str(row[0]) for row in rows if row and row[0]]
                if not ordered_ids:
                    return []

                idx_map = self.get_memory_indexes_by_ids(ordered_ids)
                return [idx_map[i] for i in ordered_ids if i in idx_map]
            except Exception as e:
                logger.warning("Engram：FTS5 BM25 检索失败，回退 LIKE：%s", e)
                return self._search_memory_indexes_by_keywords_like(
                    user_id=user_id,
                    normalized_keywords=normalized_keywords,
                    limit=limit,
                    start_time=start_time,
                    end_time=end_time,
                    source_types=source_types,
                )

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

    def get_all_group_ids(self):
        """获取所有出现过的群组ID。"""
        available_columns = self._get_table_columns(self.RawMemory)
        if "group_id" not in available_columns or not hasattr(self.RawMemory, "group_id"):
            return []
        with self.db.connection_context():
            return [
                row.group_id
                for row in self.RawMemory.select(self.RawMemory.group_id)
                .where(self.RawMemory.group_id.is_null(False))
                .distinct()
                if str(row.group_id or "").strip()
            ]
    
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

    # ========== 删除历史持久化 ==========

    def save_delete_history(self, **kwargs):
        with self.db.connection_context():
            row = self.DeleteHistory.create(**kwargs)
            return row.id

    def get_last_delete_history(self, scope_key):
        with self.db.connection_context():
            return (
                self.DeleteHistory.select()
                .where(
                    (self.DeleteHistory.scope_key == scope_key)
                    & (self.DeleteHistory.is_restored == False)
                )
                .order_by(self.DeleteHistory.deleted_at.desc(), self.DeleteHistory.id.desc())
                .first()
            )

    def mark_delete_history_restored(self, record_id):
        with self.db.connection_context():
            return (
                self.DeleteHistory.update(
                    is_restored=True,
                    restored_at=datetime.datetime.now(),
                )
                .where(self.DeleteHistory.id == record_id)
                .execute()
            )

    # ========== 向量补偿任务持久化 ==========

    def enqueue_pending_vector_jobs(self, rows):
        if not rows:
            return 0
        now = datetime.datetime.now()
        payload = []
        for item in rows:
            index_id = str(item.get("index_id", "")).strip()
            if not index_id:
                continue
            payload.append({
                "index_id": index_id,
                "user_id": str(item.get("user_id", "") or "") or None,
                "source_type": str(item.get("source_type", "") or "private") or "private",
                "summary": str(item.get("summary", "") or ""),
                "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                "retry_count": int(item.get("retry_count", 0) or 0),
                "reason": str(item.get("reason", "") or ""),
                "queued_at": item.get("queued_at") if isinstance(item.get("queued_at"), datetime.datetime) else now,
            })

        if not payload:
            return 0

        with self.db.connection_context():
            self.PendingVectorJob.insert_many(payload).execute()
        return len(payload)

    def get_pending_vector_jobs(self, limit=200):
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 200

        with self.db.connection_context():
            query = (
                self.PendingVectorJob.select()
                .order_by(self.PendingVectorJob.queued_at.asc(), self.PendingVectorJob.id.asc())
                .limit(limit)
            )
            return list(query)

    def delete_pending_vector_jobs(self, index_ids):
        if not index_ids:
            return 0
        ids = [str(i).strip() for i in index_ids if str(i).strip()]
        if not ids:
            return 0
        with self.db.connection_context():
            return (
                self.PendingVectorJob.delete()
                .where(self.PendingVectorJob.index_id << ids)
                .execute()
            )


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
        "get_all_group_ids",
        "get_all_users_stats",
        "save_delete_history",
        "get_last_delete_history",
        "mark_delete_history_restored",
        "enqueue_pending_vector_jobs",
        "get_pending_vector_jobs",
        "delete_pending_vector_jobs",
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
