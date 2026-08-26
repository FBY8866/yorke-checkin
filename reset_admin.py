"""管理员密码重置工具（服务器端）

用法：
    # 重置默认账号 浙江吉瑞 的密码为 ZJJR123456
    docker exec yorke-checkin python /app/reset_admin.py

    # 重置指定账号的密码
    docker exec yorke-checkin python /app/reset_admin.py <username> <new_password>
"""
import os
import sys
import sqlite3
from werkzeug.security import generate_password_hash

DB_PATH = os.environ.get('DATA_DIR', '/var/data/yorke') + '/checkin.db'


def main():
    if len(sys.argv) >= 3:
        username = sys.argv[1]
        new_password = sys.argv[2]
    else:
        username = '浙江吉瑞'
        new_password = 'ZJJR123456'

    if not os.path.exists(DB_PATH):
        print(f'❌ 找不到数据库文件: {DB_PATH}')
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # 兼容：表可能尚未创建（极早期部署）
    cur.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    new_hash = generate_password_hash(new_password)
    cur.execute('SELECT id FROM admins WHERE username = ?', (username,))
    row = cur.fetchone()
    if row:
        cur.execute('UPDATE admins SET password_hash = ? WHERE username = ?', (new_hash, username))
        action = '已重置'
    else:
        cur.execute('INSERT INTO admins (username, password_hash) VALUES (?, ?)', (username, new_hash))
        action = '已创建'
    conn.commit()
    conn.close()
    print(f'✅ {action}管理员 [{username}] 的密码 (新密码: {new_password})')


if __name__ == '__main__':
    main()
