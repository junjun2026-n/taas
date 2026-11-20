# ============================================================
# 文件名称：mysql_backend.py
# 生成日期：2026-11-19
# 修改日期：2026-11-20
# 版本信息：v0.261115
# 作    者：JUN
# 备    注：本程序由AI辅助完成。
#           文件功能：实现 mysql_backend 模块相关功能。
#           文件用途：供TAAS的 taas_api/backend/app/mysql_backend.py 模块使用。
#           注意事项：运行前检查依赖与配置；涉及数据或服务变更时先备份并验证。
# ============================================================
"""MySQL 8.0.16+ compatibility for the platform's parameterized SQL.

PostgreSQL connections bypass this module. Values are always bound by the driver.
"""
import json
import re
import queue
from datetime import datetime, timezone
from contextlib import contextmanager
from threading import Lock

import pymysql
import sqlglot
from sqlglot import exp


def translate(statement):
    statement = re.sub(r'\bBIGSERIAL\b', 'BIGINT AUTO_INCREMENT', statement, flags=re.I)
    statement = re.sub(r'\bSERIAL\b', 'INTEGER AUTO_INCREMENT', statement, flags=re.I)
    statement = re.sub(r'\bTIMESTAMPTZ\b', 'DATETIME(6)', statement, flags=re.I)
    statement = re.sub(r'\bDEFAULT\s+now\(\)', 'DEFAULT CURRENT_TIMESTAMP(6)', statement, flags=re.I)
    statement = re.sub(r'\bTIMESTAMP\b(?!\s*\()', 'DATETIME(6)', statement, flags=re.I)
    statement = re.sub(r'\bCURRENT_TIMESTAMP\b(?!\s*\()', 'CURRENT_TIMESTAMP(6)', statement, flags=re.I)
    statement = re.sub(r'\bJSONB\b', 'JSON', statement, flags=re.I)
    statement = re.sub(r'\bILIKE\b', 'LIKE', statement, flags=re.I)
    statement = re.sub(r'::(?:jsonb?|text|integer|int|regclass)(?:\[\])?', '', statement, flags=re.I)
    statement = re.sub(r"\b(TEXT|JSON)\s+(NOT NULL\s+)?DEFAULT\s+('(?:[^']|'')*')", lambda m: m[1]+' '+(m[2] or '')+'DEFAULT ('+m[3]+')', statement, flags=re.I)
    statement = re.sub(r'\bTEXT\s+(PRIMARY KEY|UNIQUE)', r'VARCHAR(191) \1', statement, flags=re.I)
    statement = re.sub(r'(`?payment_trade_no`?)\s+TEXT\b', r'\1 VARCHAR(191)', statement, flags=re.I)
    if re.match(r'CREATE TABLE',statement,re.I):
        foreign=[]
        def reference(match):
            foreign.append(f'FOREIGN KEY (`{match[1]}`) REFERENCES `{match[3]}`({match[4]}) ON DELETE {match[5]}')
            return match[1]+match[2]
        statement=re.sub(r'\b(\w+)(\s+(?:BIGINT|INTEGER)(?:\s+NOT NULL)?(?:\s+UNIQUE)?)\s+REFERENCES\s+(\w+)\((\w+)\)\s+ON DELETE\s+(SET NULL|CASCADE|RESTRICT)',reference,statement,flags=re.I)
        if foreign:
            end=statement.rfind(')');statement=statement[:end]+', '+', '.join(foreign)+statement[end:]
    statement = re.sub(r'ON CONFLICT\s*(?:\([^)]*\))?\s*DO NOTHING', 'ON DUPLICATE KEY UPDATE __platform_noop__=__platform_noop__', statement, flags=re.I)
    statement = re.sub(r'ON CONFLICT\s*\([^)]*\)\s*DO UPDATE SET', 'ON DUPLICATE KEY UPDATE', statement, flags=re.I)
    statement = re.sub(r'\bEXCLUDED\.([a-z_][a-z_0-9]*)', r'VALUES(\1)', statement, flags=re.I)
    statement = re.sub(r"\bINTERVAL\s+'(\d+)\s+(\w+)'", lambda m:'INTERVAL '+m[1]+' '+m[2].rstrip('s').upper(), statement, flags=re.I)
    statement = re.sub(r"(\w+)\s*\*\s*INTERVAL\s+1\s+DAY", r'INTERVAL \1 DAY', statement, flags=re.I)
    statement = re.sub(r'\b([\w.]+)\s*=\s*ANY\(%s\)', r'\1 IN %s', statement, flags=re.I)
    statement = re.sub(r"%s\s*=\s*ANY\(string_to_array\((\w+),\s*','\)\)", r"FIND_IN_SET(%s,\1)>0", statement, flags=re.I)
    return statement


class Cursor:
    def __init__(self, connection, dictionaries=False):
        self.connection = connection
        self.dictionaries = dictionaries
        self.raw = connection.raw.cursor()
        self.rows = None
        self.description = None
        self.rowcount = -1
        self.lastrowid = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self.raw.close()

    def _format(self, row):
        if row is None: return None
        values = list(row)
        for i, column in enumerate(self.description or ()):
            if column[1] == 245 and isinstance(values[i], str):
                values[i] = json.loads(values[i])
            if self.connection.dictionaries and isinstance(values[i],datetime) and values[i].tzinfo is None:
                values[i]=values[i].replace(tzinfo=timezone.utc)
        return dict(zip([d[0] for d in self.description], values)) if self.dictionaries else tuple(values)

    def fetchone(self):
        if self.rows is not None:
            return self.rows.pop(0) if self.rows else None
        return self._format(self.raw.fetchone())
    def fetchall(self):
        if self.rows is not None:
            rows, self.rows = self.rows, []
            return rows
        return [self._format(row) for row in self.raw.fetchall()]
    def __iter__(self): return iter(self.fetchall())

    def execute(self, sql, params=None):
        params = tuple(json.dumps(v.adapted, ensure_ascii=False) if hasattr(v,'adapted') else tuple(v) if isinstance(v,list) else v for v in (params or ()))
        # Parse statement boundaries without splitting quoted semicolons.
        statements = sqlglot.tokenize(sql, read='postgres')
        boundaries = [t.start for t in statements if t.token_type.name == 'SEMICOLON']
        parts=[]; start=0
        for end in boundaries: parts.append(sql[start:end]); start=end+1
        parts.append(sql[start:])
        offset=0
        for statement in parts:
            statement=re.sub(r'^\s*(?:--[^\n]*\n\s*)*','',statement).strip()
            if not statement: continue
            count=len(re.findall(r'(?<!%)%s',statement)); values=params[offset:offset+count];offset+=count
            self._execute(statement, values)
        return self

    def _execute(self, statement, values):
        self.rows=None
        if re.match(r'(CREATE SCHEMA|SET (LOCAL )?statement_timeout|SELECT pg_advisory_xact_lock)',statement,re.I):
            self.rows=[];self.description=None;self.rowcount=0;return
        # PostgreSQL schema migrations become metadata-checked MySQL migrations.
        add=re.match(r'ALTER TABLE\s+(\w+)\s+ADD COLUMN IF NOT EXISTS\s+(\w+)\s+(.*)',statement,re.I|re.S)
        if add:
            self.raw.execute('SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s',add.group(1,2))
            if self.raw.fetchone(): self.rows=[];self.description=None;self.rowcount=0;return
            statement=f'ALTER TABLE `{add[1]}` ADD COLUMN `{add[2]}` {add[3]}'
        idx=re.match(r'CREATE (UNIQUE )?INDEX IF NOT EXISTS (\w+)\s+ON\s+(\w+)(.*)',statement,re.I|re.S)
        if idx:
            self.raw.execute('SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=%s AND index_name=%s',idx.group(3,2))
            if self.raw.fetchone(): self.rows=[];self.description=None;self.rowcount=0;return
            tail=idx[4]
            # Nullable unique keys naturally permit multiple NULLs in MySQL.
            if re.search(r'\bWHERE\b',tail,re.I):
                if not idx[1]:
                    pass
                elif re.search(r"WHERE\s+(\w+)\s*<>\s*''\s*$",tail,re.I):
                    field=re.search(r"WHERE\s+(\w+)",tail,re.I)[1]
                    tail=f"((NULLIF(`{field}`,'')))"
                elif not re.search(r'WHERE\s+\w+\s+IS NOT NULL\s*$',tail,re.I):
                    raise ValueError('MySQL does not support this partial index: '+idx[2])
                tail=re.split(r'\bWHERE\b',tail,flags=re.I)[0]
            statement=f'CREATE {idx[1] or ""}INDEX `{idx[2]}` ON `{idx[3]}`{tail}'
        drop=re.match(r'DROP INDEX IF EXISTS (\w+)',statement,re.I)
        if drop:
            self.raw.execute('SELECT table_name FROM information_schema.statistics WHERE table_schema=DATABASE() AND index_name=%s',(drop[1],))
            row=self.raw.fetchone()
            if row:self.raw.execute(f'DROP INDEX `{drop[1]}` ON `{row[0]}`')
            self.rows=[];self.description=None;self.rowcount=0;return
        returning=re.search(r'\s+RETURNING\s+(.+)$',statement,re.I|re.S)
        fields=returning[1] if returning else None
        if returning:statement=statement[:returning.start()]
        table_match=re.match(r'(?:INSERT INTO|UPDATE|DELETE FROM)\s+(\w+)',statement,re.I)
        table=table_match[1] if table_match else None
        before=None;where=None;where_values=()
        if fields and re.match(r'(UPDATE|DELETE)',statement,re.I):
            where=re.search(r'\bWHERE\b(.+)',statement,re.I|re.S)
            if not where:raise ValueError('MySQL RETURNING emulation requires a WHERE clause')
            where=where[1];n=len(re.findall(r'(?<!%)%s',where));where_values=values[-n:] if n else ()
            self.raw.execute(translate(f'SELECT {fields} FROM `{table}` WHERE {where} FOR UPDATE'),where_values)
            before=self.raw.fetchall();before_description=self.raw.description
        rendered=translate(statement)
        if '__platform_noop__' in rendered:
            self.raw.execute(f'SHOW COLUMNS FROM `{table}`');column=self.raw.fetchone()[0]
            rendered=rendered.replace('__platform_noop__=__platform_noop__',f'`{column}`=`{table}`.`{column}`')
        # AST conversion handles casts, concatenation, date truncation, FILTER,
        # JSON extraction and DISTINCT ON without touching bound values.
        if not re.match(r'(CREATE|ALTER|DROP)',rendered,re.I):
            try:
                parsed=sqlglot.parse_one(rendered.replace('%s', ':__bind'),read='postgres')
                for node in list(parsed.find_all(exp.Filter)):
                    agg=node.this;condition=node.expression.this
                    argument=agg.this
                    if isinstance(agg,exp.Count) and isinstance(argument,exp.Star): argument=exp.Literal.number(1)
                    agg.set('this',exp.Case(ifs=[exp.If(this=condition,true=argument)], default=exp.Null()))
                    node.replace(agg)
                rendered=parsed.sql(dialect='mysql').replace(':__bind','%s')
            except Exception:
                # Seed statements containing localized strings or vendor syntax
                # are already valid MySQL after the targeted rewrites above.
                pass
        try:self.raw.execute(rendered,values)
        except pymysql.MySQLError as exc:
            raise type(exc)(exc.args[0], str(exc.args[1])+' [SQL: '+rendered[:1400]+']') from exc
        self.description=self.raw.description;self.rowcount=self.raw.rowcount;self.lastrowid=self.raw.lastrowid
        if not fields:return
        if statement.upper().startswith('DELETE'):
            self.description=before_description;self.rows=[self._format(row) for row in before];return
        if statement.upper().startswith('UPDATE'):
            self.raw.execute(translate(f'SELECT {fields} FROM `{table}` WHERE {where}'),where_values)
        elif self.rowcount == 0 and re.search(r'DO NOTHING',statement,re.I):
            self.rows=[];self.description=(('empty',None),);return
        elif self.lastrowid:
            self.raw.execute(f'SELECT {fields} FROM `{table}` WHERE id=%s',(self.lastrowid,))
        else:
            cols=re.search(r'INSERT INTO\s+\w+\s*\(([^)]+)\)',statement,re.I)
            columns=[x.strip() for x in cols[1].split(',')]
            # Natural primary keys (permissions, settings, model prices).
            self.raw.execute(f'SHOW KEYS FROM `{table}` WHERE Key_name=\'PRIMARY\'')
            primary=[row[4] for row in self.raw.fetchall()]
            conflict=re.search(r'ON CONFLICT\s*\(([^)]+)\)',statement,re.I)
            if conflict:primary=[x.strip() for x in conflict[1].split(',')]
            binds=[values[columns.index(c)] for c in primary]
            self.raw.execute(f'SELECT {fields} FROM `{table}` WHERE '+ ' AND '.join(f'`{c}`=%s' for c in primary),binds)
        self.description=self.raw.description;self.rows=[self._format(row) for row in self.raw.fetchall()]

    def executemany(self, sql, rows):
        count=0
        for values in rows:self.execute(sql,values);count+=max(self.rowcount,0)
        self.rowcount=count


class Connection:
    def __init__(self, raw, dictionaries=False): self.raw=raw;self.dictionaries=dictionaries
    @property
    def closed(self):return not self.raw.open
    def cursor(self, cursor_factory=None):return Cursor(self, self.dictionaries or cursor_factory is not None)
    def commit(self):self.raw.commit()
    def rollback(self):self.raw.rollback()
    def close(self):self.raw.close()
    def __enter__(self):return self
    def __exit__(self, kind,*args):self.rollback() if kind else self.commit()


def connect(settings, dictionaries=False):
    tls=settings.get('sslmode','prefer')
    if tls in ('verify-ca','verify-full'):
        raise ValueError('MySQL CA validation requires an ssl_ca certificate; use require or disable')
    raw=pymysql.connect(host=settings['host'],port=int(settings.get('port',3306)),database=settings.get('dbname') or settings['database'],user=settings['user'],password=settings.get('password',''),charset='utf8mb4',autocommit=False,connect_timeout=8,read_timeout=60,write_timeout=60,ssl={'check_hostname':False} if tls=='require' else None,init_command="SET SESSION time_zone='+00:00', sql_mode='STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ANSI_QUOTES'")
    return Connection(raw,dictionaries)


class Pool:
    def __init__(self, settings, maximum=20, dictionaries=False):
        self.settings=dict(settings);self.maximum=maximum;self.dictionaries=dictionaries
        self.idle=queue.Queue();self.lock=Lock();self.total=0;self.closed=False
    def getconn(self):
        with self.lock:
            if self.closed:raise RuntimeError('Database pool is closed')
            try:conn=self.idle.get_nowait()
            except queue.Empty:
                if self.total<self.maximum:
                    conn=connect(self.settings,self.dictionaries);self.total+=1
                else:conn=None
        if conn is None:conn=self.idle.get(timeout=10)
        try:conn.raw.ping(reconnect=False)
        except Exception:
            conn.close()
            with self.lock:self.total-=1
            return self.getconn()
        return conn
    def putconn(self, conn, close=False):
        with self.lock:
            if close or self.closed:conn.close();self.total-=1
            else:self.idle.put_nowait(conn)
    def closeall(self):
        with self.lock:
            self.closed=True
            while not self.idle.empty():self.idle.get_nowait().close();self.total-=1
