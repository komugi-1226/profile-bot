import os
import asyncpg
import datetime
import logging

# データベース接続URLを環境変数から取得
DATABASE_URL = os.environ.get('DATABASE_URL')
# データベース接続プールをグローバル変数として保持
_pool = None

async def get_pool():
    """
    データベース接続プールを取得する。
    プールが存在しないか、閉じられている場合は新しいプールを作成する。
    """
    global _pool
    if _pool is None or _pool._closed:
        if not DATABASE_URL:
            raise ValueError("DATABASE_URL environment variable is not set.")
        
        # pgbouncerなどのコネクションプーラーと互換性を持たせるため、
        # statement_cache_size=0 を設定する。
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=30,
            statement_cache_size=0  # pgbouncer互換性のため追加
        )
        logging.info("✅ 新しいデータベース接続プールを作成しました (pgbouncer対応)")
    return _pool

async def close_pool():
    """
    データベース接続プールを安全に閉じる。
    """
    global _pool
    if _pool and not _pool._closed:
        await _pool.close()
        _pool = None
        logging.info("✅ データベース接続プールを閉じました")

async def init_db():
    """
    BUMPくん機能用のテーブルを初期化する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                bump_count INTEGER NOT NULL DEFAULT 0
            );
        ''')
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                remind_at TIMESTAMP WITH TIME ZONE NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting'
            );
        ''')
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        ''')
        await connection.execute('''
            INSERT INTO settings (key, value) VALUES ('scan_completed', 'false')
            ON CONFLICT (key) DO NOTHING;
        ''')
    logging.info("✅ BUMPくん用テーブルを初期化しました")

async def is_scan_completed():
    pool = await get_pool()
    async with pool.acquire() as connection:
        record = await connection.fetchrow("SELECT value FROM settings WHERE key = 'scan_completed'")
    return record and record['value'] == 'true'

async def mark_scan_as_completed():
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute("UPDATE settings SET value = 'true' WHERE key = 'scan_completed'")

async def record_bump(user_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('''
            INSERT INTO users (user_id, bump_count) VALUES ($1, 1)
            ON CONFLICT (user_id) DO UPDATE SET bump_count = users.bump_count + 1;
        ''', user_id)
        count = await connection.fetchval('SELECT bump_count FROM users WHERE user_id = $1', user_id)
    return count

async def get_top_users(limit=5):
    pool = await get_pool()
    async with pool.acquire() as connection:
        records = await connection.fetch(
            'SELECT user_id, bump_count FROM users ORDER BY bump_count DESC LIMIT $1', limit
        )
    return records

async def get_user_count(user_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        count = await connection.fetchval('SELECT bump_count FROM users WHERE user_id = $1', user_id)
    return count or 0

async def set_reminder(channel_id, remind_time):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('DELETE FROM reminders')
        await connection.execute('INSERT INTO reminders (channel_id, remind_at) VALUES ($1, $2)', channel_id, remind_time)

async def get_reminder():
    pool = await get_pool()
    async with pool.acquire() as connection:
        record = await connection.fetchrow(
            'SELECT channel_id, remind_at, status FROM reminders ORDER BY remind_at LIMIT 1'
        )
    return record

async def update_reminder_status(channel_id, new_status):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            'UPDATE reminders SET status = $1 WHERE channel_id = $2', new_status, channel_id
        )

async def clear_reminder():
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('DELETE FROM reminders')

async def get_total_bumps():
    pool = await get_pool()
    async with pool.acquire() as connection:
        total = await connection.fetchval('SELECT SUM(bump_count) FROM users')
    return total or 0

async def init_intro_bot_db():
    """
    自己紹介Bot用のデータベーステーブルを初期化または更新する。
    テーブルが存在しない場合は新規作成し、古いスキーマ（テーブル構造）の場合は
    'created_at'カラムを自動的に追加して互換性を保つ。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        # [変更前のコード]
        # CREATE TABLE IF NOT EXISTS のみだったため、既にテーブルが存在し、
        # かつそのテーブルに 'created_at' カラムがない場合にエラーが発生していた。
        #
        # [変更後のコード]
        # 1. まず、最新のスキーマでテーブル作成を試みる (IF NOT EXISTS)。
        # 2. 次に、'created_at' カラムが存在するかを明示的にチェックする。
        # 3. もし存在しなければ、ALTER TABLE を使ってカラムを追加する。
        #
        # [変更理由]
        # RenderからSupabaseへの移行など、異なる環境で作成された古いデータベースに接続した際に、
        # 'column "created_at" does not exist' というエラーが発生する問題を解決するため。
        # この修正により、Bot起動時にデータベースのテーブル構造が自動的に更新され、
        # エラーを未然に防ぐことができる（自己修復機能）。

        # 1. テーブルが存在しない場合に備えて、最新の定義で作成を試みる
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS introductions (
                user_id BIGINT PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # 2. 'created_at' カラムの存在をチェック
        column_exists = await connection.fetchval('''
            SELECT EXISTS (
                SELECT 1
                FROM   information_schema.columns
                WHERE  table_name = 'introductions'
                AND    column_name = 'created_at'
            );
        ''')

        # 3. カラムが存在しない場合のみ、追加処理を実行
        if not column_exists:
            logging.info("📝 'introductions'テーブルに'created_at'カラムが存在しないため、追加処理を実行します...")
            await connection.execute('''
                ALTER TABLE introductions
                ADD COLUMN created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
            ''')
            logging.info("✅ 'created_at'カラムの追加が完了しました。")

        # ユーザーID検索を高速化するためのインデックスを作成（存在しない場合のみ）
        await connection.execute('''
            CREATE INDEX IF NOT EXISTS idx_introductions_user_id ON introductions(user_id);
        ''')
    logging.info("✅ 自己紹介Bot用テーブルを初期化しました")

async def save_intro(user_id, channel_id, message_id):
    """
    ユーザーの自己紹介情報をデータベースに保存または更新する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        # 更新か新規作成かをログで区別するために、先に存在チェックを行う
        existing = await connection.fetchrow(
            "SELECT user_id FROM introductions WHERE user_id = $1", user_id
        )
        
        # INSERT ... ON CONFLICT を使い、レコードが存在すればUPDATE、なければINSERTを実行する。
        # これにより、コードがシンプルになり、アトミックな操作が保証される。
        # created_atをCURRENT_TIMESTAMPで更新することで、最新の投稿日時を記録する。
        await connection.execute('''
            INSERT INTO introductions (user_id, channel_id, message_id, created_at) 
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET 
                channel_id = EXCLUDED.channel_id, 
                message_id = EXCLUDED.message_id, 
                created_at = EXCLUDED.created_at;
        ''', user_id, channel_id, message_id)
        
        if existing:
            logging.debug(f"🔄 自己紹介を更新: User {user_id}")
        else:
            logging.info(f"🆕 新しい自己紹介を保存: User {user_id}")

async def get_intro_ids(user_id):
    """
    ユーザーIDに基づいて、自己紹介のチャンネルIDとメッセージIDを取得する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        record = await connection.fetchrow(
            "SELECT channel_id, message_id FROM introductions WHERE user_id = $1", user_id
        )
    
    if record:
        logging.debug(f"✅ 自己紹介発見: User {user_id}")
    else:
        logging.debug(f"❌ 自己紹介未発見: User {user_id}")
    
    return record

async def get_intro_count():
    """
    データベースに保存されている自己紹介の総数を取得する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        count = await connection.fetchval("SELECT COUNT(*) FROM introductions")
    return count or 0

async def list_recent_intros(limit=10):
    """
    最近投稿された自己紹介を最大指定件数まで取得する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        records = await connection.fetch(
            "SELECT user_id, channel_id, message_id, created_at FROM introductions ORDER BY created_at DESC LIMIT $1",
            limit
        )
    return records

async def init_shugoshin_db():
    """
    守護神ボット機能用のテーブルを初期化する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                report_id SERIAL PRIMARY KEY, guild_id BIGINT, message_id BIGINT,
                target_user_id BIGINT, violated_rule TEXT, details TEXT,
                message_link TEXT, urgency TEXT, status TEXT DEFAULT '未対応',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id BIGINT PRIMARY KEY,
                report_channel_id BIGINT,
                urgent_role_id BIGINT
            );
        ''')
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS report_cooldowns (
                user_id BIGINT PRIMARY KEY,
                last_report_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
        ''')
    logging.info("✅ 守護神ボット用テーブルを初期化しました")

async def setup_guild(guild_id, report_channel_id, urgent_role_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('''
            INSERT INTO guild_settings (guild_id, report_channel_id, urgent_role_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id) DO UPDATE
            SET report_channel_id = $2, urgent_role_id = $3;
        ''', guild_id, report_channel_id, urgent_role_id)

async def get_guild_settings(guild_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        settings = await connection.fetchrow(
            "SELECT report_channel_id, urgent_role_id FROM guild_settings WHERE guild_id = $1",
            guild_id
        )
    return settings

async def check_cooldown(user_id, cooldown_seconds):
    pool = await get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await connection.fetchrow(
                "SELECT last_report_at FROM report_cooldowns WHERE user_id = $1", user_id
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            if record:
                time_since_last = now - record['last_report_at']
                if time_since_last.total_seconds() < cooldown_seconds:
                    return cooldown_seconds - time_since_last.total_seconds()
            await connection.execute('''
                INSERT INTO report_cooldowns (user_id, last_report_at) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET last_report_at = $2;
            ''', user_id, now)
            return 0

async def create_report(guild_id, target_user_id, violated_rule, details, message_link, urgency):
    pool = await get_pool()
    async with pool.acquire() as connection:
        report_id = await connection.fetchval(
            '''INSERT INTO reports (guild_id, target_user_id, violated_rule, details, message_link, urgency) 
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING report_id''',
            guild_id, target_user_id, violated_rule, details, message_link, urgency
        )
    return report_id

async def update_report_message_id(report_id, message_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE reports SET message_id = $1 WHERE report_id = $2",
            message_id, report_id
        )

async def update_report_status(report_id, new_status):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE reports SET status = $1 WHERE report_id = $2",
            new_status, report_id
        )

async def get_report(report_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        record = await connection.fetchrow("SELECT * FROM reports WHERE report_id = $1", report_id)
    return record

async def list_reports(status_filter=None):
    pool = await get_pool()
    query = "SELECT report_id, target_user_id, status FROM reports"
    params = []
    if status_filter and status_filter != 'all':
        query += " WHERE status = $1"
        params.append(status_filter)
    query += " ORDER BY report_id DESC LIMIT 20"
    async with pool.acquire() as connection:
        records = await connection.fetch(query, *params)
    return records

async def get_report_stats():
    """
    レポートのステータスごとの件数を集計して取得する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        stats = await connection.fetch('''
            SELECT status, COUNT(*) as count 
            FROM reports 
            GROUP BY status
        ''')
    # 取得したレコードのリストを {'ステータス名': 件数} の形式の辞書に変換して返す
    return {row['status']: row['count'] for row in stats}
```filepath: ...` は、私がコードを提示する際に使用するマークダウンの書式です。この書式ごと `database.py` ファイルにコピーしてしまったため、Pythonが解釈できない不正な構文としてエラーを引き起こしています。
    また、添付されたファイルを見ると、`get_report_stats` 関数の最後の行が `for row` で終わっており、`in stats}` が欠けているように見えます。これも構文エラーの原因となります。

### 修正方針

1.  ファイルに誤ってコピーされたマークダウンの書式 ````...` を削除します。
2.  `get_report_stats` 関数の最後の行を修正し、正しい辞書内包表記 `... for row in stats}` にします。
3.  これらの修正を反映した `database.py` の全コードを提供します。

### 修正後のコード (全文)

以下のコードをコピーし、現在の `c:\Users\tomim\Desktop\GitHub\profile-bot\database.py` の内容を完全に置き換えてください。

````python
// filepath: c:\Users\tomim\Desktop\GitHub\profile-bot\database.py
import os
import asyncpg
import datetime
import logging

# データベース接続URLを環境変数から取得
DATABASE_URL = os.environ.get('DATABASE_URL')
# データベース接続プールをグローバル変数として保持
_pool = None

async def get_pool():
    """
    データベース接続プールを取得する。
    プールが存在しないか、閉じられている場合は新しいプールを作成する。
    """
    global _pool
    if _pool is None or _pool._closed:
        if not DATABASE_URL:
            raise ValueError("DATABASE_URL environment variable is not set.")
        
        # pgbouncerなどのコネクションプーラーと互換性を持たせるため、
        # statement_cache_size=0 を設定する。
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=30,
            statement_cache_size=0  # pgbouncer互換性のため追加
        )
        logging.info("✅ 新しいデータベース接続プールを作成しました (pgbouncer対応)")
    return _pool

async def close_pool():
    """
    データベース接続プールを安全に閉じる。
    """
    global _pool
    if _pool and not _pool._closed:
        await _pool.close()
        _pool = None
        logging.info("✅ データベース接続プールを閉じました")

async def init_db():
    """
    BUMPくん機能用のテーブルを初期化する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                bump_count INTEGER NOT NULL DEFAULT 0
            );
        ''')
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                remind_at TIMESTAMP WITH TIME ZONE NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting'
            );
        ''')
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        ''')
        await connection.execute('''
            INSERT INTO settings (key, value) VALUES ('scan_completed', 'false')
            ON CONFLICT (key) DO NOTHING;
        ''')
    logging.info("✅ BUMPくん用テーブルを初期化しました")

async def is_scan_completed():
    pool = await get_pool()
    async with pool.acquire() as connection:
        record = await connection.fetchrow("SELECT value FROM settings WHERE key = 'scan_completed'")
    return record and record['value'] == 'true'

async def mark_scan_as_completed():
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute("UPDATE settings SET value = 'true' WHERE key = 'scan_completed'")

async def record_bump(user_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('''
            INSERT INTO users (user_id, bump_count) VALUES ($1, 1)
            ON CONFLICT (user_id) DO UPDATE SET bump_count = users.bump_count + 1;
        ''', user_id)
        count = await connection.fetchval('SELECT bump_count FROM users WHERE user_id = $1', user_id)
    return count

async def get_top_users(limit=5):
    pool = await get_pool()
    async with pool.acquire() as connection:
        records = await connection.fetch(
            'SELECT user_id, bump_count FROM users ORDER BY bump_count DESC LIMIT $1', limit
        )
    return records

async def get_user_count(user_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        count = await connection.fetchval('SELECT bump_count FROM users WHERE user_id = $1', user_id)
    return count or 0

async def set_reminder(channel_id, remind_time):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('DELETE FROM reminders')
        await connection.execute('INSERT INTO reminders (channel_id, remind_at) VALUES ($1, $2)', channel_id, remind_time)

async def get_reminder():
    pool = await get_pool()
    async with pool.acquire() as connection:
        record = await connection.fetchrow(
            'SELECT channel_id, remind_at, status FROM reminders ORDER BY remind_at LIMIT 1'
        )
    return record

async def update_reminder_status(channel_id, new_status):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            'UPDATE reminders SET status = $1 WHERE channel_id = $2', new_status, channel_id
        )

async def clear_reminder():
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('DELETE FROM reminders')

async def get_total_bumps():
    pool = await get_pool()
    async with pool.acquire() as connection:
        total = await connection.fetchval('SELECT SUM(bump_count) FROM users')
    return total or 0

async def init_intro_bot_db():
    """
    自己紹介Bot用のデータベーステーブルを初期化または更新する。
    テーブルが存在しない場合は新規作成し、古いスキーマ（テーブル構造）の場合は
    'created_at'カラムを自動的に追加して互換性を保つ。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        # [変更前のコード]
        # CREATE TABLE IF NOT EXISTS のみだったため、既にテーブルが存在し、
        # かつそのテーブルに 'created_at' カラムがない場合にエラーが発生していた。
        #
        # [変更後のコード]
        # 1. まず、最新のスキーマでテーブル作成を試みる (IF NOT EXISTS)。
        # 2. 次に、'created_at' カラムが存在するかを明示的にチェックする。
        # 3. もし存在しなければ、ALTER TABLE を使ってカラムを追加する。
        #
        # [変更理由]
        # RenderからSupabaseへの移行など、異なる環境で作成された古いデータベースに接続した際に、
        # 'column "created_at" does not exist' というエラーが発生する問題を解決するため。
        # この修正により、Bot起動時にデータベースのテーブル構造が自動的に更新され、
        # エラーを未然に防ぐことができる（自己修復機能）。

        # 1. テーブルが存在しない場合に備えて、最新の定義で作成を試みる
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS introductions (
                user_id BIGINT PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        ''')

        # 2. 'created_at' カラムの存在をチェック
        column_exists = await connection.fetchval('''
            SELECT EXISTS (
                SELECT 1
                FROM   information_schema.columns
                WHERE  table_name = 'introductions'
                AND    column_name = 'created_at'
            );
        ''')

        # 3. カラムが存在しない場合のみ、追加処理を実行
        if not column_exists:
            logging.info("📝 'introductions'テーブルに'created_at'カラムが存在しないため、追加処理を実行します...")
            await connection.execute('''
                ALTER TABLE introductions
                ADD COLUMN created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
            ''')
            logging.info("✅ 'created_at'カラムの追加が完了しました。")

        # ユーザーID検索を高速化するためのインデックスを作成（存在しない場合のみ）
        await connection.execute('''
            CREATE INDEX IF NOT EXISTS idx_introductions_user_id ON introductions(user_id);
        ''')
    logging.info("✅ 自己紹介Bot用テーブルを初期化しました")

async def save_intro(user_id, channel_id, message_id):
    """
    ユーザーの自己紹介情報をデータベースに保存または更新する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        # 更新か新規作成かをログで区別するために、先に存在チェックを行う
        existing = await connection.fetchrow(
            "SELECT user_id FROM introductions WHERE user_id = $1", user_id
        )
        
        # INSERT ... ON CONFLICT を使い、レコードが存在すればUPDATE、なければINSERTを実行する。
        # これにより、コードがシンプルになり、アトミックな操作が保証される。
        # created_atをCURRENT_TIMESTAMPで更新することで、最新の投稿日時を記録する。
        await connection.execute('''
            INSERT INTO introductions (user_id, channel_id, message_id, created_at) 
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET 
                channel_id = EXCLUDED.channel_id, 
                message_id = EXCLUDED.message_id, 
                created_at = EXCLUDED.created_at;
        ''', user_id, channel_id, message_id)
        
        if existing:
            logging.debug(f"🔄 自己紹介を更新: User {user_id}")
        else:
            logging.info(f"🆕 新しい自己紹介を保存: User {user_id}")

async def get_intro_ids(user_id):
    """
    ユーザーIDに基づいて、自己紹介のチャンネルIDとメッセージIDを取得する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        record = await connection.fetchrow(
            "SELECT channel_id, message_id FROM introductions WHERE user_id = $1", user_id
        )
    
    if record:
        logging.debug(f"✅ 自己紹介発見: User {user_id}")
    else:
        logging.debug(f"❌ 自己紹介未発見: User {user_id}")
    
    return record

async def get_intro_count():
    """
    データベースに保存されている自己紹介の総数を取得する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        count = await connection.fetchval("SELECT COUNT(*) FROM introductions")
    return count or 0

async def list_recent_intros(limit=10):
    """
    最近投稿された自己紹介を最大指定件数まで取得する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        records = await connection.fetch(
            "SELECT user_id, channel_id, message_id, created_at FROM introductions ORDER BY created_at DESC LIMIT $1",
            limit
        )
    return records

async def init_shugoshin_db():
    """
    守護神ボット機能用のテーブルを初期化する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                report_id SERIAL PRIMARY KEY, guild_id BIGINT, message_id BIGINT,
                target_user_id BIGINT, violated_rule TEXT, details TEXT,
                message_link TEXT, urgency TEXT, status TEXT DEFAULT '未対応',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id BIGINT PRIMARY KEY,
                report_channel_id BIGINT,
                urgent_role_id BIGINT
            );
        ''')
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS report_cooldowns (
                user_id BIGINT PRIMARY KEY,
                last_report_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
        ''')
    logging.info("✅ 守護神ボット用テーブルを初期化しました")

async def setup_guild(guild_id, report_channel_id, urgent_role_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute('''
            INSERT INTO guild_settings (guild_id, report_channel_id, urgent_role_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id) DO UPDATE
            SET report_channel_id = $2, urgent_role_id = $3;
        ''', guild_id, report_channel_id, urgent_role_id)

async def get_guild_settings(guild_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        settings = await connection.fetchrow(
            "SELECT report_channel_id, urgent_role_id FROM guild_settings WHERE guild_id = $1",
            guild_id
        )
    return settings

async def check_cooldown(user_id, cooldown_seconds):
    pool = await get_pool()
    async with pool.acquire() as connection:
        async with connection.transaction():
            record = await connection.fetchrow(
                "SELECT last_report_at FROM report_cooldowns WHERE user_id = $1", user_id
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            if record:
                time_since_last = now - record['last_report_at']
                if time_since_last.total_seconds() < cooldown_seconds:
                    return cooldown_seconds - time_since_last.total_seconds()
            await connection.execute('''
                INSERT INTO report_cooldowns (user_id, last_report_at) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET last_report_at = $2;
            ''', user_id, now)
            return 0

async def create_report(guild_id, target_user_id, violated_rule, details, message_link, urgency):
    pool = await get_pool()
    async with pool.acquire() as connection:
        report_id = await connection.fetchval(
            '''INSERT INTO reports (guild_id, target_user_id, violated_rule, details, message_link, urgency) 
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING report_id''',
            guild_id, target_user_id, violated_rule, details, message_link, urgency
        )
    return report_id

async def update_report_message_id(report_id, message_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE reports SET message_id = $1 WHERE report_id = $2",
            message_id, report_id
        )

async def update_report_status(report_id, new_status):
    pool = await get_pool()
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE reports SET status = $1 WHERE report_id = $2",
            new_status, report_id
        )

async def get_report(report_id):
    pool = await get_pool()
    async with pool.acquire() as connection:
        record = await connection.fetchrow("SELECT * FROM reports WHERE report_id = $1", report_id)
    return record

async def list_reports(status_filter=None):
    pool = await get_pool()
    query = "SELECT report_id, target_user_id, status FROM reports"
    params = []
    if status_filter and status_filter != 'all':
        query += " WHERE status = $1"
        params.append(status_filter)
    query += " ORDER BY report_id DESC LIMIT 20"
    async with pool.acquire() as connection:
        records = await connection.fetch(query, *params)
    return records

async def get_report_stats():
    """
    レポートのステータスごとの件数を集計して取得する。
    """
    pool = await get_pool()
    async with pool.acquire() as connection:
        stats = await connection.fetch('''
            SELECT status, COUNT(*) as count 
            FROM reports 
            GROUP BY status
        ''')
    # 取得したレコードのリストを {'ステータス名': 件数} の形式の辞書に変換して返