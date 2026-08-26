"""约克超级品牌日打卡系统 - 主应用"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import (
    Flask, render_template, request, jsonify, send_from_directory,
    redirect, url_for, session, abort, send_file
)
from PIL import Image, ImageDraw, ImageFont
import io
import uuid
import json
import urllib.request
import urllib.parse

CST = timezone(timedelta(hours=8))  # 中国时区 UTC+8（无夏令时，固定偏移）


def now_cst():
    """返回中国时区(UTC+8)当前时间，避免部署到海外服务器时日期/截止时间偏移 8 小时"""
    return datetime.now(CST)


app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'yorke-checkin-2026-secret-key-please-change')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload
# 云部署/本地通用：数据目录与上传目录可用环境变量覆盖（Render 等挂载持久盘时用）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'data'))
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', os.path.join(DATA_DIR, 'uploads'))
app.config['UPLOAD_FOLDER'] = UPLOAD_DIR


@app.after_request
def _no_cache(resp):
    """禁止浏览器/隧道缓存动态页面，确保榜单、首页及时刷新"""
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


# 确保目录存在
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, 'checkin.db')


def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表"""
    conn = get_db()
    cur = conn.cursor()

    # 成员表
    cur.execute('''
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            wechat_id TEXT,
            phone TEXT,
            avatar TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            is_admin INTEGER DEFAULT 0
        )
    ''')

    # 打卡记录表
    cur.execute('''
        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            check_date TEXT NOT NULL,        -- YYYY-MM-DD
            check_type TEXT NOT NULL,         -- morning / evening / designer / cross / oldcustomer / moment / newlead / followup
            content TEXT,
            photo_path TEXT,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            points INTEGER DEFAULT 0,
            is_valid INTEGER DEFAULT 1,
            FOREIGN KEY (member_id) REFERENCES members(id)
        )
    ''')

    # 申诉表
    cur.execute('''
        CREATE TABLE IF NOT EXISTS appeals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            checkin_id INTEGER,
            reason TEXT NOT NULL,
            status TEXT DEFAULT 'pending',  -- pending / approved / rejected
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            resolution TEXT,
            FOREIGN KEY (member_id) REFERENCES members(id)
        )
    ''')

    # 违规记录表
    cur.execute('''
        CREATE TABLE IF NOT EXISTS violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            checkin_id INTEGER,
            reason TEXT NOT NULL,
            action TEXT NOT NULL,            -- warn / zero_month / cancel_rewards
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES members(id)
        )
    ''')

    # 活动配置表
    cur.execute('''
        CREATE TABLE IF NOT EXISTS activity_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # 手动加减分记录表
    cur.execute('''
        CREATE TABLE IF NOT EXISTS score_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            points INTEGER NOT NULL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES members(id)
        )
    ''')
    # 初始化默认配置
    defaults = {
        'activity_name': '约克超级品牌日打卡系统',
        'start_date': now_cst().strftime('%Y-%m-%d'),
        'end_date': (now_cst() + timedelta(days=29)).strftime('%Y-%m-%d'),
        'morning_deadline': '09:30',
        'evening_deadline': '20:00',
        'morning_points': '1',
        'evening_points': '1',
        'optional_cap': '6',
        'standard_award_threshold': '180',
        'standard_award_amount': '200',
        'extra_award_threshold': '240',
        'extra_award_amount': '200',
        'champion_award_amount': '200',
        'champion_min_score': '220',
        'max_award_total': '600',
        'appeal_window_hours': '24',
    }
    for k, v in defaults.items():
        cur.execute('INSERT OR IGNORE INTO activity_config (key, value) VALUES (?, ?)', (k, v))

    # 迁移：打卡记录增加定位字段（兼容旧库，已存在则跳过）
    cur.execute('PRAGMA table_info(checkins)')
    existing_cols = {r[1] for r in cur.fetchall()}
    for col, col_type in (('lat', 'REAL'), ('lng', 'REAL'), ('address', 'TEXT')):
        if col not in existing_cols:
            cur.execute(f'ALTER TABLE checkins ADD COLUMN {col} {col_type}')

    # 迁移：成员增加公司字段
    cur.execute('PRAGMA table_info(members)')
    mcols = {r[1] for r in cur.fetchall()}
    if 'company' not in mcols:
        cur.execute('ALTER TABLE members ADD COLUMN company TEXT')

    # 迁移：打卡增加客户姓名字段（仅拜访类录入）
    cur.execute('PRAGMA table_info(checkins)')
    ccols = {r[1] for r in cur.fetchall()}
    if 'customer_name' not in ccols:
        cur.execute('ALTER TABLE checkins ADD COLUMN customer_name TEXT')

    # 公司（经销商）表：老板端独立密码，终端后台可改
    cur.execute('''
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            boss_password TEXT
        )
    ''')
    # 种子 27 家经销商（INSERT OR IGNORE 保证幂等，已改过的密码不会被覆盖）
    for cname in DEALER_COMPANIES:
        cur.execute('INSERT OR IGNORE INTO companies (name, boss_password) VALUES (?, ?)',
                    (cname, DEFAULT_COMPANY_PASSWORD))

    # 一次性清空历史演示数据（傅兵宇等旧成员），仅首次启动执行，之后重启不再清空
    cur.execute("SELECT value FROM activity_config WHERE key='wiped_v2'")
    _w = cur.fetchone()
    if not _w or _w[0] != '1':
        cur.execute('DELETE FROM score_adjustments')
        cur.execute('DELETE FROM violations')
        cur.execute('DELETE FROM appeals')
        cur.execute('DELETE FROM checkins')
        cur.execute('DELETE FROM members')
        cur.execute("INSERT OR REPLACE INTO activity_config (key, value) VALUES ('wiped_v2','1')")

    conn.commit()
    conn.close()


def get_companies():
    """返回所有经销商公司名（按 id 顺序），供登录/老板端下拉使用"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT name FROM companies ORDER BY id')
    names = [r[0] for r in cur.fetchall()]
    conn.close()
    return names


# ============ 评分规则配置 ============
# 选做项每类每日封顶
OPTIONAL_CAPS = {
    'designer': 2,    # 设计师拜访 1分/次 封顶2分
    'cross': 2,       # 异业拜访 1分/次 封顶2分
    'oldcustomer': 1, # 老顾客维护 1分/次 封顶1分
    'moment': 1,      # 朋友圈发布 1分/条 封顶1分
    'newlead': 2,     # 新增意向客户 1分/个 封顶2分
    'followup': 2,    # 顾客跟进谈单 1分/次 封顶2分
}
OPTIONAL_PER_UNIT = 1  # 每个动作1分
OPTIONAL_TOTAL_CAP = 6  # 选做项当日总分封顶
REQUIRED_TYPES = ('morning', 'evening')
MORNING_POINTS = 1
EVENING_POINTS = 1

# ============ 公司与权限配置 ============
# 经销商（老板端）默认密码，终端后台(/admin)可逐家修改
DEFAULT_COMPANY_PASSWORD = 'yk2026'
# 27 家经销商公司名单（来自区域总代理下发，作为下拉选项与数据隔离边界）
DEALER_COMPANIES = [
    '缙云县壶镇镇名锐家电经营部',
    '金华吉佳环境科技有限公司',
    '缙云县新铭暖通设备店',
    '丽水市锐鹏电器有限公司',
    '龙泉市约克家电经营部',
    '青田博宏电器店',
    '遂昌利安暖通商行',
    '武义佳源节能设备有限公司',
    '永康市沁心暖通设备有限公司',
    '永康市世通家电有限公司',
    '常山鑫雷制冷设备商行',
    '开化瑞兴新能源科技有限公司',
    '兰溪市越顺暖通器材商行',
    '衢州市皇诚暖通设备有限公司',
    '江山市诚宏节能环境科技有限公司',
    '龙游胜辉节能设备有限公司',
    '东阳恒峰暖通设备有限公司',
    '东阳市琦瑞成套设备有限公司',
    '东阳市横店飞军家用电器商行',
    '浦江县周道建材有限公司',
    '义乌市鸿森暖通设备有限公司',
    '义乌尚裕电器有限公司',
    '金华斯派客暖通设备有限公司',
    '义乌鼎尚暖通设备有限公司',
    '义乌新灵瑞暖通设备有限公司',
    '义乌市新誉环境设备有限公司',
    '金华市家祥暖通设备有限公司',
]


def compute_day_points(member_id, check_date):
    """计算某成员某日的有效分数（按规则引擎实时算）"""
    conn = get_db()
    cur = conn.cursor()

    # 必做项
    cur.execute('''
        SELECT check_type FROM checkins
        WHERE member_id = ? AND check_date = ? AND check_type IN ('morning','evening') AND is_valid = 1
    ''', (member_id, check_date))
    required_done = set(r['check_type'] for r in cur.fetchall())

    has_morning = 'morning' in required_done
    has_evening = 'evening' in required_done

    # 必做项：早目标、晚总结各自独立计分（互不依赖）
    required_points = 0
    if has_morning:
        required_points += MORNING_POINTS
    if has_evening:
        required_points += EVENING_POINTS

    # 选做项
    cur.execute('''
        SELECT check_type FROM checkins
        WHERE member_id = ? AND check_date = ? AND check_type NOT IN ('morning','evening') AND is_valid = 1
    ''', (member_id, check_date))
    optional_records = [r['check_type'] for r in cur.fetchall()]

    # 每类分别累计（按上限封顶）
    optional_points = 0
    type_counts = {}
    for t in optional_records:
        type_counts[t] = type_counts.get(t, 0) + 1

    for t, count in type_counts.items():
        cap = OPTIONAL_CAPS.get(t, 0)
        earned = min(count, cap) * OPTIONAL_PER_UNIT
        optional_points += earned

    # 选做总分封顶
    optional_points = min(optional_points, OPTIONAL_TOTAL_CAP)

    total = required_points + optional_points
    conn.close()
    return {
        'required_points': required_points,
        'optional_points': optional_points,
        'total': total,
        'has_morning': has_morning,
        'has_evening': has_evening,
    }


def recompute_and_save_points(member_id, check_date):
    """重新计算并保存某日所有相关打卡记录的 points 字段"""
    result = compute_day_points(member_id, check_date)
    conn = get_db()
    cur = conn.cursor()
    # 标记打分结果到当日记录上（总分=成员当日总分）
    cur.execute('''
        UPDATE checkins SET points = ?
        WHERE member_id = ? AND check_date = ?
    ''', (result['total'], member_id, check_date))
    conn.commit()
    conn.close()
    return result


def get_adjustment_total(member_id):
    """获取某成员的手动加减分合计（可正可负）"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COALESCE(SUM(points), 0) FROM score_adjustments WHERE member_id = ?', (member_id,))
    val = cur.fetchone()[0]
    conn.close()
    return int(val or 0)


def reverse_geocode(lat, lng):
    """根据经纬度反查中文地址（OpenStreetMap Nominatim，免费无需 key）。
    失败或超时返回 None，由调用方降级处理。"""
    if not lat or not lng:
        return None
    try:
        params = urllib.parse.urlencode({
            'format': 'jsonv2',
            'lat': lat,
            'lon': lng,
            'accept-language': 'zh-CN',
            'zoom': 18,
        })
        url = 'https://nominatim.openstreetmap.org/reverse?' + params
        req = urllib.request.Request(url, headers={'User-Agent': 'YorkeCheckin/1.0 (internal)'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
        address = data.get('display_name')
        # 取前两段（省/市/区）即可，过长则截断
        if address:
            parts = [p.strip() for p in address.split(',')]
            short = ''.join(parts[:3])
            return short[:60]
        return None
    except Exception:
        return None


def get_cjk_font(size):
    """返回支持中文的字体，找不到则回退到默认字体（避免中文水印变方块）"""
    candidates = [
        'C:/Windows/Fonts/msyh.ttc',          # Windows 微软雅黑
        'C:/Windows/Fonts/simhei.ttf',        # Windows 黑体
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',  # Linux 文泉驿
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def add_watermark_pil(img_bytes, text, coord_text=None):
    """服务端给照片加水印：底部半透明条(姓名+时间)，右上角『约克打卡』标签，可选 GPS 坐标。
    返回处理后的 JPEG 字节；任何异常均回退原图，不阻断打卡。"""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        # 限制最大宽度，省存储 + 加快处理
        max_w = 1280
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)))
        draw = ImageDraw.Draw(img)
        bar_h = 72
        draw.rectangle([0, img.height - bar_h, img.width, img.height], fill=(0, 0, 0))
        f1 = get_cjk_font(26)
        draw.text((16, img.height - 50), text, font=f1, fill=(255, 255, 255))
        if coord_text:
            f2 = get_cjk_font(20)
            draw.text((16, img.height - 22), coord_text, font=f2, fill=(209, 250, 229))
        draw.rectangle([img.width - 120, 10, img.width - 10, 48], fill=(30, 64, 175))
        f3 = get_cjk_font(20)
        draw.text((img.width - 65, 29), '约克打卡', font=f3, fill=(255, 255, 255), anchor='mm')
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=85)
        return buf.getvalue()
    except Exception:
        return img_bytes


# ============ 路由 ============

@app.route('/')
def index():
    """主页 - 根据登录状态分流"""
    member_id = session.get('member_id')
    is_admin = session.get('is_admin', False)
    if member_id:
        return redirect(url_for('home'))
    return render_template('login.html', companies=get_companies())


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        company = request.form.get('company', '').strip()
        phone = request.form.get('phone', '').strip()
        admin_code = request.form.get('admin_code', '').strip()

        if not name:
            return render_template('login.html', error='请输入姓名', companies=get_companies())

        conn = get_db()
        cur = conn.cursor()
        # 检查是否是管理员（终端后台，全量可改）
        is_admin = (admin_code == 'yorke2026')

        if is_admin:
            cur.execute('SELECT * FROM members WHERE name = ? AND is_admin = 1', (name,))
            existing = cur.fetchone()
            if existing:
                session['member_id'] = existing['id']
                session['member_name'] = existing['name']
                session['is_admin'] = True
                conn.close()
                return redirect(url_for('admin_dashboard'))

            # 创建管理员
            uid = f"admin-{uuid.uuid4().hex[:8]}"
            cur.execute('''
                INSERT INTO members (uid, name, company, phone, is_admin)
                VALUES (?, ?, ?, ?, 1)
            ''', (uid, name, company, phone))
            conn.commit()
            member_id = cur.lastrowid
            session['member_id'] = member_id
            session['member_name'] = name
            session['is_admin'] = True
            conn.close()
            return redirect(url_for('admin_dashboard'))
        else:
            # 成员登录：按「公司 + 姓名」识别同一用户，避免不同公司重名导致账号分裂、跨公司串数据
            cur.execute('SELECT * FROM members WHERE company = ? AND name = ? AND is_admin = 0 AND is_active = 1', (company, name))
            existing = cur.fetchone()
            if existing:
                if phone:
                    cur.execute('UPDATE members SET phone = COALESCE(NULLIF(?, ""), phone) WHERE id = ?',
                                (phone, existing['id']))
                    conn.commit()
                session['member_id'] = existing['id']
                session['member_name'] = existing['name']
                session['is_admin'] = False
                conn.close()
                return redirect(url_for('home'))

            # 公司必须来自下拉名单（companies 表），防止乱填
            cur.execute('SELECT 1 FROM companies WHERE name = ?', (company,))
            if not cur.fetchone():
                conn.close()
                return render_template('login.html', error='请在下拉列表中选择有效的公司', companies=get_companies())
            # 创建新成员
            uid = f"m-{uuid.uuid4().hex[:8]}"
            cur.execute('''
                INSERT INTO members (uid, name, company, phone, is_admin)
                VALUES (?, ?, ?, ?, 0)
            ''', (uid, name, company, phone))
            conn.commit()
            member_id = cur.lastrowid
            session['member_id'] = member_id
            session['member_name'] = name
            session['is_admin'] = False
            conn.close()
            return redirect(url_for('home'))

    return render_template('login.html', companies=get_companies())


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/home')
def home():
    """成员主页"""
    if not session.get('member_id'):
        return redirect(url_for('index'))
    if session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))

    member_id = session['member_id']
    today = now_cst().strftime('%Y-%m-%d')
    now_time = now_cst().strftime('%H:%M')

    # 今日状态
    today_result = compute_day_points(member_id, today)

    # 累计
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(DISTINCT check_date) FROM checkins WHERE member_id = ? AND is_valid = 1', (member_id,))
    total_days = cur.fetchone()[0]

    # 累计总分
    cur.execute('''
        SELECT DISTINCT check_date FROM checkins
        WHERE member_id = ? AND is_valid = 1
    ''', (member_id,))
    all_dates = [r[0] for r in cur.fetchall()]

    total_score = 0
    for d in all_dates:
        result = compute_day_points(member_id, d)
        total_score += result['total']
    total_score += get_adjustment_total(member_id)

    # 今日打卡明细（含定位地址），用于首页展示
    cur.execute('''
        SELECT check_type, content, photo_path, submitted_at, points, address
        FROM checkins
        WHERE member_id = ? AND check_date = ? AND is_valid = 1
        ORDER BY submitted_at ASC
    ''', (member_id, today))
    today_records = [dict(r) for r in cur.fetchall()]

    conn.close()

    return render_template('home.html',
                           name=session['member_name'],
                           today=today_result,
                           now_time=now_time,
                           total_days=total_days,
                           total_score=total_score,
                           today_records=today_records)


@app.route('/api/checkin', methods=['POST'])
def api_checkin():
    """打卡接口"""
    if not session.get('member_id'):
        return jsonify({'ok': False, 'error': '请先登录'}), 401

    member_id = session['member_id']
    check_type = request.form.get('check_type', '')
    content = request.form.get('content', '').strip()
    customer_name = request.form.get('customer_name', '').strip()
    photo = request.files.get('photo')

    # 定位信息（前端 geolocation 传入，可选）
    try:
        lat = float(request.form.get('lat')) if request.form.get('lat') else None
        lng = float(request.form.get('lng')) if request.form.get('lng') else None
    except (ValueError, TypeError):
        lat, lng = None, None
    address = reverse_geocode(lat, lng)

    now = now_cst()
    today = now.strftime('%Y-%m-%d')
    now_time = now.strftime('%H:%M')

    # 选做项必须要有照片 + 内容；必做项内容必填
    optional_types = ['designer', 'cross', 'oldcustomer', 'moment', 'newlead', 'followup']
    required_types = ['morning', 'evening']
    deadline_map = {'evening': '20:00'}

    if check_type not in required_types and check_type not in optional_types:
        return jsonify({'ok': False, 'error': '未知的打卡类型'}), 400

    # 必做项截止时间校验（仅晚总结）
    if check_type in deadline_map:
        deadline = deadline_map[check_type]
        if now_time > deadline:
            return jsonify({'ok': False, 'error': f'{check_type} 打卡已过截止时间 {deadline}'}), 400

    # 防重复打卡：同一 check_type 同一天只能打一次
    _conn = get_db()
    _cur = _conn.cursor()
    _cur.execute(
        'SELECT COUNT(*) FROM checkins WHERE member_id=? AND check_date=? AND check_type=? AND is_valid=1',
        (member_id, today, check_type))
    if _cur.fetchone()[0] > 0:
        _conn.close()
        return jsonify({'ok': False, 'error': '今日该类型已打卡，请勿重复'}), 400
    _conn.close()

    # 选做项要求照片
    if check_type in optional_types:
        if not photo or not photo.filename:
            return jsonify({'ok': False, 'error': '选做项必须上传照片'}), 400
        if not content:
            return jsonify({'ok': False, 'error': '请填写说明文字'}), 400
        if not customer_name:
            return jsonify({'ok': False, 'error': '请填写客户姓名'}), 400

    # 必做项要求内容
    if check_type in required_types and not content:
        return jsonify({'ok': False, 'error': '请填写打卡内容'}), 400

    # 保存照片（服务端加水印：时间+姓名+坐标，避免客户端 canvas 处理失败导致传不上图）
    photo_path = None
    if photo and photo.filename:
        raw = photo.read()
        name = session.get('member_name', '')
        ts = now.strftime('%Y-%m-%d %H:%M')
        coord_text = f"GPS {lat:.4f}, {lng:.4f}" if (lat is not None and lng is not None) else None
        wm = add_watermark_pil(raw, f"{name} · {ts}", coord_text)
        fname = f"{member_id}_{today}_{check_type}_{uuid.uuid4().hex[:6]}.jpg"
        fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
        with open(fpath, 'wb') as f:
            f.write(wm)
        photo_path = f'uploads/{fname}'

    # 写入记录
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO checkins (member_id, check_date, check_type, content, photo_path, lat, lng, address, customer_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (member_id, today, check_type, content, photo_path, lat, lng, address, customer_name))
    conn.commit()

    # 重新计算当日分
    result = recompute_and_save_points(member_id, today)

    conn.close()
    return jsonify({
        'ok': True,
        'message': f'打卡成功！今日已获 {result["total"]} 分',
        'today': result,
    })


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/api/today_checkins')
def api_today_checkins():
    """查询今日打卡明细"""
    if not session.get('member_id'):
        return jsonify({'ok': False}), 401
    member_id = session['member_id']
    today = now_cst().strftime('%Y-%m-%d')
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT check_type, content, photo_path, submitted_at, points
        FROM checkins
        WHERE member_id = ? AND check_date = ? AND is_valid = 1
        ORDER BY submitted_at ASC
    ''', (member_id, today))
    records = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({'ok': True, 'records': records, 'today': compute_day_points(member_id, today)})


def build_leaderboard():
    """计算累计总榜与今日日榜，返回 (rows, today_rows, today)"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, name FROM members WHERE is_admin = 0 AND is_active = 1')
    members = [dict(r) for r in cur.fetchall()]

    # 累计总榜
    rows = []
    for m in members:
        cur2 = conn.cursor()
        cur2.execute('''
            SELECT DISTINCT check_date FROM checkins
            WHERE member_id = ? AND is_valid = 1
        ''', (m['id'],))
        dates = [r[0] for r in cur2.fetchall()]
        total = 0
        for d in dates:
            total += compute_day_points(m['id'], d)['total']
        total += get_adjustment_total(m['id'])
        rows.append({'name': m['name'], 'total': total})
    rows.sort(key=lambda x: -x['total'])

    # 今日日榜：按今日(当天)得分排序，仅列今日有打卡分的人
    today = now_cst().strftime('%Y-%m-%d')
    today_rows = []
    for m in members:
        day = compute_day_points(m['id'], today)
        if day['total'] > 0:
            today_rows.append({'name': m['name'], 'total': day['total']})
    today_rows.sort(key=lambda x: -x['total'])

    conn.close()
    return rows, today_rows, today


@app.route('/leaderboard')
def leaderboard():
    """公开榜单（首屏服务端渲染，后续由前端 AJAX 每 20s 自动刷新）"""
    rows, today_rows, today = build_leaderboard()
    me = session.get('member_name')
    return render_template('leaderboard.html', rows=rows, today_rows=today_rows, today=today, me=me)


@app.route('/api/leaderboard')
def api_leaderboard():
    """榜单 JSON 接口，供前端定时刷新，避免整页重载导致的不及时"""
    rows, today_rows, today = build_leaderboard()
    return jsonify({'ok': True, 'rows': rows, 'today_rows': today_rows, 'today': today})


@app.route('/admin')
def admin_dashboard():
    """管理员仪表盘"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))

    msg = request.args.get('msg')
    error = request.args.get('error')
    filter_company = request.args.get('company', '').strip()

    conn = get_db()
    cur = conn.cursor()

    # 概览数据
    cur.execute('SELECT COUNT(*) FROM members WHERE is_admin = 0 AND is_active = 1')
    total_members = cur.fetchone()[0]

    today = now_cst().strftime('%Y-%m-%d')
    cur.execute('SELECT COUNT(DISTINCT member_id) FROM checkins WHERE check_date = ? AND is_valid = 1', (today,))
    today_active = cur.fetchone()[0]

    cur.execute('SELECT COUNT(*) FROM checkins WHERE check_date = ? AND check_type = ?', (today, 'morning'))
    morning_count = cur.fetchone()[0]

    cur.execute('SELECT COUNT(*) FROM checkins WHERE check_date = ? AND check_type = ?', (today, 'evening'))
    evening_count = cur.fetchone()[0]

    # 全部成员 + 累计分（可按公司筛选）
    if filter_company:
        cur.execute('SELECT id, name, company, phone, joined_at FROM members WHERE is_admin = 0 AND is_active = 1 AND company = ? ORDER BY id', (filter_company,))
    else:
        cur.execute('SELECT id, name, company, phone, joined_at FROM members WHERE is_admin = 0 AND is_active = 1 ORDER BY id')
    members = [dict(r) for r in cur.fetchall()]

    rows = []
    for m in members:
        cur2 = conn.cursor()
        cur2.execute('''
            SELECT DISTINCT check_date FROM checkins
            WHERE member_id = ? AND is_valid = 1
        ''', (m['id'],))
        dates = [r[0] for r in cur2.fetchall()]
        total = 0
        for d in dates:
            r = compute_day_points(m['id'], d)
            total += r['total']
        total += get_adjustment_total(m['id'])

        # 计算连续天数
        dates_sorted = sorted(dates)
        streak = 0
        if dates_sorted:
            check_d = datetime.strptime(today, '%Y-%m-%d')
            while check_d.strftime('%Y-%m-%d') in dates:
                streak += 1
                check_d -= timedelta(days=1)

        # 今日状态
        today_result = compute_day_points(m['id'], today)

        rows.append({
            'id': m['id'],
            'name': m['name'],
            'company': m['company'] or '未分配',
            'total': total,
            'streak': streak,
            'today': today_result['total'],
            'has_morning': today_result['has_morning'],
            'has_evening': today_result['has_evening'],
        })

    rows.sort(key=lambda x: -x['total'])

    # 奖励档位标记
    for r in rows:
        if r['total'] >= 220:
            r['award'] = '冠军奖(已达标)'
        elif r['total'] >= 240:
            r['award'] = '超额+冠军'
        elif r['total'] >= 180:
            r['award'] = '达标奖'
        else:
            r['award'] = '-'

    # 公司（经销商）管理列表
    cur.execute('SELECT id, name, boss_password FROM companies ORDER BY id')
    companies_mgmt = [dict(r) for r in cur.fetchall()]

    conn.close()
    return render_template('admin.html',
                           name=session['member_name'],
                           total_members=total_members,
                           today_active=today_active,
                           morning_count=morning_count,
                           evening_count=evening_count,
                           rows=rows,
                           today=today,
                           companies=companies_mgmt,
                           company_options=get_companies(),
                           filter_company=filter_company,
                           msg=msg,
                           error=error)


@app.route('/admin/member/<int:member_id>')
def admin_member_detail(member_id):
    """查看某成员详情"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT * FROM members WHERE id = ?', (member_id,))
    member = dict(cur.fetchone() or {})

    cur.execute('''
        SELECT * FROM checkins WHERE member_id = ? ORDER BY check_date DESC, submitted_at DESC
    ''', (member_id,))
    records = [dict(r) for r in cur.fetchall()]

    msg = request.args.get('msg')
    error = request.args.get('error')
    conn.close()
    return render_template('member_detail.html', member=member, records=records, msg=msg, error=error)


@app.route('/admin/member/add', methods=['POST'])
def admin_member_add():
    """后台添加成员"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    name = request.form.get('name', '').strip()
    company = request.form.get('company', '').strip()
    phone = request.form.get('phone', '').strip()
    if not name:
        return redirect(url_for('admin_dashboard', error='请输入姓名'))
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM members WHERE name=? AND company=? AND is_admin=0 AND is_active=1',
                (name, company))
    if cur.fetchone():
        conn.close()
        return redirect(url_for('admin_dashboard', error='该成员已存在（同名同公司）'))
    uid = f"m-{uuid.uuid4().hex[:8]}"
    cur.execute(
        'INSERT INTO members (uid, name, company, phone, is_admin) VALUES (?, ?, ?, ?, 0)',
        (uid, name, company, phone))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard', msg='已添加成员：' + name))


@app.route('/admin/member/<int:member_id>/delete', methods=['POST'])
def admin_member_delete(member_id):
    """后台软删除成员（保留打卡记录）"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE members SET is_active=0 WHERE id=? AND is_admin=0', (member_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard', msg='已软删除该成员（打卡记录保留）'))


@app.route('/admin/member/<int:member_id>/edit', methods=['POST'])
def admin_member_edit(member_id):
    """后台编辑成员资料"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    name = request.form.get('name', '').strip()
    company = request.form.get('company', '').strip()
    phone = request.form.get('phone', '').strip()
    if not name:
        return redirect(url_for('admin_member_detail', member_id=member_id, error='姓名不能为空'))
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE members SET name=?, company=?, phone=? WHERE id=? AND is_admin=0',
                (name, company, phone, member_id))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_member_detail', member_id=member_id, msg='资料已更新'))


@app.route('/admin/member/<int:member_id>/adjust', methods=['POST'])
def admin_member_adjust(member_id):
    """后台手动加减分"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    try:
        points = int(request.form.get('points', '0'))
    except (ValueError, TypeError):
        return redirect(url_for('admin_member_detail', member_id=member_id, error='分数必须是数字'))
    if points == 0:
        return redirect(url_for('admin_member_detail', member_id=member_id, error='分数不能为 0'))
    note = request.form.get('note', '').strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT INTO score_adjustments (member_id, points, note) VALUES (?, ?, ?)',
                (member_id, points, note))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_member_detail', member_id=member_id,
                            msg=('已加' if points > 0 else '已减') + '分 ' + str(abs(points))))


@app.route('/admin/company/edit', methods=['POST'])
def admin_company_edit():
    """终端后台修改某经销商老板端密码"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))
    cid = request.form.get('company_id', '').strip()
    pw = request.form.get('boss_password', '').strip()
    if not cid or not pw:
        return redirect(url_for('admin_dashboard', error='公司与密码均不能为空'))
    conn = get_db()
    cur = conn.cursor()
    cur.execute('UPDATE companies SET boss_password = ? WHERE id = ?', (pw, cid))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard', msg='已更新该公司老板端密码'))


# ============ 老板端（经销商，只读、仅本公司） ============

@app.route('/boss/login', methods=['GET', 'POST'])
def boss_login():
    if request.method == 'POST':
        company = request.form.get('company', '').strip()
        pw = request.form.get('password', '').strip()
        if not company or not pw:
            return render_template('boss_login.html', error='请选择公司并输入密码', companies=get_companies())
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT * FROM companies WHERE name = ?', (company,))
        row = cur.fetchone()
        conn.close()
        if not row or row['boss_password'] != pw:
            return render_template('boss_login.html', error='公司或密码错误', companies=get_companies())
        session['boss_company'] = company
        session['is_boss'] = True
        return redirect(url_for('boss_dashboard'))
    return render_template('boss_login.html', companies=get_companies())


@app.route('/boss/dashboard')
def boss_dashboard():
    if not session.get('is_boss'):
        return redirect(url_for('boss_login'))
    company = session['boss_company']
    date = request.args.get('date', now_cst().strftime('%Y-%m-%d')).strip()

    conn = get_db()
    cur = conn.cursor()
    # 本公司销售名单
    cur.execute('SELECT id, name, company FROM members WHERE company = ? AND is_admin = 0 AND is_active = 1 ORDER BY name', (company,))
    members = [dict(r) for r in cur.fetchall()]
    mids = [m['id'] for m in members]

    summary = {'total_members': len(members), 'today_active': 0, 'visits': 0, 'customers': 0}
    records = []
    if mids:
        ph = ','.join('?' * len(mids))
        cur.execute(f'SELECT COUNT(DISTINCT member_id) FROM checkins WHERE member_id IN ({ph}) AND check_date = ? AND is_valid = 1', mids + [date])
        summary['today_active'] = cur.fetchone()[0]
        cur.execute(f'''SELECT m.name AS member_name, c.check_type, c.customer_name, c.content,
                       c.address, c.photo_path, c.submitted_at, c.points
                       FROM checkins c JOIN members m ON c.member_id = m.id
                       WHERE c.member_id IN ({ph}) AND c.check_date = ? AND c.is_valid = 1
                       ORDER BY c.submitted_at ASC''', mids + [date])
        records = [dict(r) for r in cur.fetchall()]
        cur.execute(f'''SELECT COUNT(*) FROM checkins WHERE member_id IN ({ph}) AND check_date = ?
                       AND is_valid = 1 AND check_type NOT IN ('morning','evening')''', mids + [date])
        summary['visits'] = cur.fetchone()[0]
        cur.execute(f'''SELECT COUNT(DISTINCT customer_name) FROM checkins WHERE member_id IN ({ph})
                       AND check_date = ? AND is_valid = 1 AND customer_name IS NOT NULL AND customer_name != '' ''', mids + [date])
        summary['customers'] = cur.fetchone()[0]

    # 各销售累计分
    for m in members:
        cur2 = conn.cursor()
        cur2.execute('SELECT DISTINCT check_date FROM checkins WHERE member_id = ? AND is_valid = 1', (m['id'],))
        dates = [r[0] for r in cur2.fetchall()]
        total = 0
        for d in dates:
            total += compute_day_points(m['id'], d)['total']
        total += get_adjustment_total(m['id'])
        m['total'] = total
        m['today'] = compute_day_points(m['id'], date)['total']

    conn.close()
    return render_template('boss_dashboard.html',
                           company=company, date=date, members=members,
                           records=records, summary=summary,
                           today=now_cst().strftime('%Y-%m-%d'))


@app.route('/admin/leaderboard_image')
def admin_leaderboard_image():
    """生成并下载榜单图"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, name FROM members WHERE is_admin = 0 AND is_active = 1')
    members = [dict(r) for r in cur.fetchall()]

    today = now_cst().strftime('%Y-%m-%d')
    rows = []
    for m in members:
        cur2 = conn.cursor()
        cur2.execute('''
            SELECT DISTINCT check_date FROM checkins
            WHERE member_id = ? AND is_valid = 1
        ''', (m['id'],))
        dates = [r[0] for r in cur2.fetchall()]
        total = 0
        for d in dates:
            r = compute_day_points(m['id'], d)
            total += r['total']
        total += get_adjustment_total(m['id'])

        # 今日分
        today_result = compute_day_points(m['id'], today)

        # 连续天数
        dates_set = set(dates)
        streak = 0
        check_d = datetime.strptime(today, '%Y-%m-%d')
        while check_d.strftime('%Y-%m-%d') in dates_set:
            streak += 1
            check_d -= timedelta(days=1)

        rows.append({
            'name': m['name'],
            'total': total,
            'today': today_result['total'],
            'streak': streak,
        })

    rows.sort(key=lambda x: -x['total'])
    conn.close()

    # 生成图片
    img = render_leaderboard_image(rows)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png', as_attachment=True,
                     download_name=f'leaderboard_{now_cst().strftime("%Y%m%d")}.png')


@app.route('/admin/leaderboard_view')
def admin_leaderboard_view():
    """在浏览器中渲染榜单图（用于手动截图）"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, name FROM members WHERE is_admin = 0 AND is_active = 1')
    members = [dict(r) for r in cur.fetchall()]

    rows = []
    today = now_cst().strftime('%Y-%m-%d')
    for m in members:
        cur2 = conn.cursor()
        cur2.execute('''
            SELECT DISTINCT check_date FROM checkins
            WHERE member_id = ? AND is_valid = 1
        ''', (m['id'],))
        dates = [r[0] for r in cur2.fetchall()]
        total = 0
        for d in dates:
            r = compute_day_points(m['id'], d)
            total += r['total']
        total += get_adjustment_total(m['id'])

        # 今日分
        today_result = compute_day_points(m['id'], today)

        # 连续
        dates_sorted = sorted(dates)
        streak = 0
        if dates_sorted:
            check_d = datetime.strptime(today, '%Y-%m-%d')
            while check_d.strftime('%Y-%m-%d') in dates:
                streak += 1
                check_d -= timedelta(days=1)

        rows.append({
            'name': m['name'],
            'total': total,
            'today': today_result['total'],
            'streak': streak,
        })

    rows.sort(key=lambda x: -x['total'])
    conn.close()

    return render_template('leaderboard_view.html',
                           rows=rows,
                           today=today)


def render_leaderboard_image(rows):
    """用 PIL 生成榜单 PNG（无需 headless 浏览器）"""
    width = 720
    # 每行高度
    row_height = 56
    header_height = 200
    footer_height = 60
    height = header_height + max(len(rows), 1) * row_height + footer_height

    img = Image.new('RGB', (width, height), '#F5F7FA')
    draw = ImageDraw.Draw(img)

    # 尝试加载中文字体
    def get_font(size):
        candidates = [
            'C:/Windows/Fonts/msyh.ttc',
            'C:/Windows/Fonts/msyh.ttf',
            'C:/Windows/Fonts/simhei.ttf',
            '/System/Library/Fonts/PingFang.ttc',
            '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
        ]
        for c in candidates:
            if os.path.exists(c):
                try:
                    return ImageFont.truetype(c, size)
                except Exception:
                    pass
        return ImageFont.load_default()

    font_title = get_font(32)
    font_subtitle = get_font(18)
    font_row = get_font(22)
    font_small = get_font(16)

    # 头部背景
    draw.rectangle([(0, 0), (width, header_height)], fill='#1E40AF')

    # 标题
    title = '约克超级品牌日打卡系统榜单'
    draw.text((width//2 - 180, 30), title, font=font_title, fill='white')
    today = now_cst().strftime('%Y-%m-%d')
    subtitle = f'更新于 {today}  · 共 {len(rows)} 人'
    draw.text((width//2 - 110, 80), subtitle, font=font_subtitle, fill='#E0E7FF')

    # 奖档说明
    draw.text((40, 130), '达标奖 ≥180分(¥200)  ·  超额奖 ≥240分(+¥200)  ·  冠军奖 第1名≥220分(+¥200)',
              font=font_small, fill='#FCD34D')

    # 表格
    y = header_height
    draw.rectangle([(0, y), (width, y + 40)], fill='#E0E7FF')
    draw.text((40, y + 10), '排名', font=font_row, fill='#1E40AF')
    draw.text((140, y + 10), '姓名', font=font_row, fill='#1E40AF')
    draw.text((320, y + 10), '今日', font=font_row, fill='#1E40AF')
    draw.text((420, y + 10), '累计', font=font_row, fill='#1E40AF')
    draw.text((540, y + 10), '连续', font=font_row, fill='#1E40AF')
    draw.text((640, y + 10), '奖档', font=font_row, fill='#1E40AF')
    y += 40

    for i, r in enumerate(rows):
        rank = i + 1
        # 隔行底色
        if i % 2 == 0:
            draw.rectangle([(0, y), (width, y + row_height)], fill='white')
        # 前三名奖牌色
        rank_color = '#1E40AF'
        if rank == 1:
            rank_color = '#D97706'
        elif rank == 2:
            rank_color = '#6B7280'
        elif rank == 3:
            rank_color = '#92400E'

        draw.text((50, y + 15), f'{rank}', font=font_row, fill=rank_color)
        draw.text((140, y + 15), r['name'][:12], font=font_row, fill='#111827')
        draw.text((320, y + 15), str(r['today']), font=font_row, fill='#111827')
        draw.text((420, y + 15), str(r['total']), font=font_row, fill='#1E40AF')
        draw.text((540, y + 15), f"{r['streak']}天", font=font_row, fill='#111827')

        # 奖档
        if r['total'] >= 220 and rank == 1:
            award_text = '冠军'
            award_color = '#D97706'
        elif r['total'] >= 240:
            award_text = '超额'
            award_color = '#059669'
        elif r['total'] >= 180:
            award_text = '达标'
            award_color = '#2563EB'
        else:
            award_text = '-'
            award_color = '#9CA3AF'
        draw.text((640, y + 15), award_text, font=font_row, fill=award_color)

        # 分割线
        draw.line([(20, y + row_height), (width - 20, y + row_height)], fill='#E5E7EB')
        y += row_height

    # 底部
    draw.rectangle([(0, height - footer_height), (width, height)], fill='#1E40AF')
    draw.text((width//2 - 100, height - 40),
              '每日21:00更新 · 异议24h内申诉',
              font=font_small, fill='white')

    return img


@app.route('/api/appeal', methods=['POST'])
def api_appeal():
    """提交申诉"""
    if not session.get('member_id'):
        return jsonify({'ok': False}), 401
    member_id = session['member_id']
    reason = request.form.get('reason', '').strip()
    checkin_id = request.form.get('checkin_id')
    if not reason:
        return jsonify({'ok': False, 'error': '请填写申诉原因'})
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO appeals (member_id, checkin_id, reason)
        VALUES (?, ?, ?)
    ''', (member_id, int(checkin_id) if checkin_id else None, reason))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'message': '申诉已提交，管理员将在24h内处理'})


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


if __name__ == '__main__':
    init_db()
    print('=' * 60)
    print('约克超级品牌日打卡系统已启动')
    print('访问地址: http://localhost:5000')
    print('管理员登录时使用邀请码: yorke2026')
    print('=' * 60)
    # 云部署：端口读环境变量 PORT；调试模式仅在本地 FLASK_DEBUG=1 时开启
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG') == '1'
    app.run(host='0.0.0.0', port=port, debug=debug)