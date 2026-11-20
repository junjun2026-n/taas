# ============================================================
# 文件名称：database_config.py
# 生成日期：2026-10-11
# 修改日期：2026-11-20
# 版本信息：v0.261115
# 作    者：JUN
# 备    注：本程序由AI辅助完成。
#           文件功能：实现 database_config 模块相关功能。
#           文件用途：供TAAS的 taas_api/backend/app/database_config.py 模块使用。
#           注意事项：运行前检查依赖与配置；涉及数据或服务变更时先备份并验证。
# ============================================================
import json
import os
from contextlib import closing
from pathlib import Path

import psycopg2
import pymysql
from psycopg2.extensions import parse_dsn
from pydantic import BaseModel, Field

CONFIG_FILE = Path(__file__).resolve().parents[2] / 'config' / 'database.json'
DEFAULT_URL = 'postgresql://huitian:SET_LOCALLY_BEFORE_USE@192.168.17.127:5432/qionghai'


class DatabaseConfig(BaseModel):
    database_type: str = Field(default='postgresql', pattern='^(postgresql|mysql)$')
    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    dbname: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: str = ''
    sslmode: str = Field(default='prefer', pattern='^(disable|allow|prefer|require|verify-ca|verify-full)$')


def connection_settings():
    if os.getenv('TAAS_DATABASE_URL'):
        url = os.environ['TAAS_DATABASE_URL']
        if url.startswith(('mysql://','mysql+pymysql://')):
            from urllib.parse import urlparse, unquote
            p=urlparse(url); return dict(database_type='mysql',host=p.hostname,port=p.port or 3306,dbname=(p.path or '/')[1:],user=unquote(p.username or ''),password=unquote(p.password or ''))
        return parse_dsn(url) | {'database_type':'postgresql'}
    if CONFIG_FILE.exists():
        return DatabaseConfig(**json.loads(CONFIG_FILE.read_text(encoding='utf-8'))).model_dump()
    return parse_dsn(DEFAULT_URL) | {'database_type':'postgresql'}


def test_connection(config):
    try:
        if config.database_type == 'mysql':
            from .mysql_backend import connect
            with closing(connect(config.model_dump())) as connection:
                with connection.cursor() as cursor: cursor.execute('SELECT 1'); cursor.fetchone()
            return
        with closing(psycopg2.connect(**config.model_dump(exclude={'database_type'}), connect_timeout=8)) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
    except (psycopg2.Error,pymysql.MySQLError):
        raise ValueError('数据库连接失败，请检查地址、账号、密码、网络及 SSL 配置') from None


def save_config(config):
    test_connection(config)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(config.model_dump(), ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(CONFIG_FILE)


def initialize_database(config):
    from .main import init_db
    try:
        init_db(config.model_dump())
    except (psycopg2.Error, pymysql.MySQLError, OSError, ValueError):
        raise ValueError('初始化失败，请检查目标数据库是否存在、连接及创建 schema 和数据表权限。') from None
    return dict(ok=True,detail='数据库初始化完成，已有数据及密码保留；未切换连接，未迁移历史数据。空库管理员为 admin。')
