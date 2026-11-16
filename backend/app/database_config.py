import json
import os
from contextlib import closing
from pathlib import Path

import psycopg2
from psycopg2.extensions import parse_dsn
from pydantic import BaseModel, Field

CONFIG_FILE = Path(__file__).resolve().parents[2] / 'config' / 'database.json'
DEFAULT_URL = 'postgresql://huitian:SET_LOCALLY_BEFORE_USE@192.168.17.127:5432/qionghai'


class DatabaseConfig(BaseModel):
    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    dbname: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: str = ''
    sslmode: str = Field(default='prefer', pattern='^(disable|allow|prefer|require|verify-ca|verify-full)$')


def connection_settings():
    if os.getenv('TAAS_DATABASE_URL'):
        return parse_dsn(os.environ['TAAS_DATABASE_URL'])
    if CONFIG_FILE.exists():
        return DatabaseConfig(**json.loads(CONFIG_FILE.read_text(encoding='utf-8'))).model_dump()
    return parse_dsn(DEFAULT_URL)


def test_connection(config):
    try:
        with closing(psycopg2.connect(**config.model_dump(), connect_timeout=8)) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
    except psycopg2.Error:
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
    except (psycopg2.Error, OSError):
        raise ValueError('初始化失败，请检查目标数据库是否存在、连接及创建 schema 和数据表权限。') from None
    return dict(ok=True,detail='数据库初始化完成，已有数据及密码保留；未切换连接，未迁移历史数据。空库管理员为 admin。')
