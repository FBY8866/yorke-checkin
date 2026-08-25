"""约克超级品牌日打卡系统 - 主应用"""
import os
import sqlite3
from datetime import datetime, timedelta
from flask import (
    Flask, render_template, request, jsonify, send_from_directory,
    redirect, url_for, session, abort, send_file
)
from PIL import Image, ImageDraw, ImageFont
import io
import uuid
import json

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'yorke-checkin-2026-secret-key-please-change')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload
# 云部署/本地通用：数据目录与上传目录可用环境变量覆盖（Render 等挂载持久盘时用）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'data'))
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', os.path.join(BASE_DIR, 'static', 'uploads'))
app.config['UPLOAD_FOLDER'] = UPLOAD_DIR

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

    # 初始化默认配置
    defaults = {
        'activity_name': '约克超级品牌日打卡系统',
        'start_date': datetime.now().strftime('%Y-%m-%d'),
        'end_date': (datetime.now() + timedelta(days=29)).strftime('%Y-%m-%d'),
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

    conn.commit()
    conn.close()


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

    # 必做项联动：早目标未完成 → 晚总结不计分
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


# ============ 路由 ============

@app.route('/')
def index():
    """主页 - 根据登录状态分流"""
    member_id = session.get('member_id')
    is_admin = session.get('is_admin', False)
    if member_id:
        return redirect(url_for('home'))
    return render_template('login.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        wechat_id = request.form.get('wechat_id', '').strip()
        phone = request.form.get('phone', '').strip()
        admin_code = request.form.get('admin_code', '').strip()

        if not name:
            return render_template('login.html', error='请输入姓名')

        conn = get_db()
        cur = conn.cursor()
        # 检查是否是管理员
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
                INSERT INTO members (uid, name, wechat_id, phone, is_admin)
                VALUES (?, ?, ?, ?, 1)
            ''', (uid, name, wechat_id, phone))
            conn.commit()
            member_id = cur.lastrowid
            session['member_id'] = member_id
            session['member_name'] = name
            session['is_admin'] = True
            conn.close()
            return redirect(url_for('admin_dashboard'))
        else:
            # 成员登录：通过姓名+微信号匹配
            cur.execute('SELECT * FROM members WHERE name = ? AND wechat_id = ? AND is_admin = 0',
                        (name, wechat_id))
            existing = cur.fetchone()
            if existing:
                session['member_id'] = existing['id']
                session['member_name'] = existing['name']
                session['is_admin'] = False
                conn.close()
                return redirect(url_for('home'))

            # 创建新成员
            uid = f"m-{uuid.uuid4().hex[:8]}"
            cur.execute('''
                INSERT INTO members (uid, name, wechat_id, phone, is_admin)
                VALUES (?, ?, ?, ?, 0)
            ''', (uid, name, wechat_id, phone))
            conn.commit()
            member_id = cur.lastrowid
            session['member_id'] = member_id
            session['member_name'] = name
            session['is_admin'] = False
            conn.close()
            return redirect(url_for('home'))

    return render_template('login.html')


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
    today = datetime.now().strftime('%Y-%m-%d')
    now_time = datetime.now().strftime('%H:%M')

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

    conn.close()

    return render_template('home.html',
                           name=session['member_name'],
                           today=today_result,
                           now_time=now_time,
                           total_days=total_days,
                           total_score=total_score)


@app.route('/api/checkin', methods=['POST'])
def api_checkin():
    """打卡接口"""
    if not session.get('member_id'):
        return jsonify({'ok': False, 'error': '请先登录'}), 401

    member_id = session['member_id']
    check_type = request.form.get('check_type', '')
    content = request.form.get('content', '').strip()
    photo = request.files.get('photo')

    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    now_time = now.strftime('%H:%M')

    # 选做项必须要有照片 + 内容；必做项内容必填
    optional_types = ['designer', 'cross', 'oldcustomer', 'moment', 'newlead', 'followup']
    required_types_map = {'morning': '09:30', 'evening': '20:00'}

    if check_type not in required_types_map and check_type not in optional_types:
        return jsonify({'ok': False, 'error': '未知的打卡类型'}), 400

    # 必做项截止时间校验
    if check_type in required_types_map:
        deadline = required_types_map[check_type]
        if now_time > deadline:
            return jsonify({'ok': False, 'error': f'{check_type} 打卡已过截止时间 {deadline}'}), 400

    # 选做项要求照片
    if check_type in optional_types:
        if not photo or not photo.filename:
            return jsonify({'ok': False, 'error': '选做项必须上传照片'}), 400
        if not content:
            return jsonify({'ok': False, 'error': '请填写说明文字'}), 400

    # 必做项要求内容
    if check_type in required_types_map and not content:
        return jsonify({'ok': False, 'error': '请填写打卡内容'}), 400

    # 保存照片
    photo_path = None
    if photo and photo.filename:
        ext = os.path.splitext(photo.filename)[1] or '.jpg'
        fname = f"{member_id}_{today}_{check_type}_{uuid.uuid4().hex[:6]}{ext}"
        fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
        photo.save(fpath)
        photo_path = f'uploads/{fname}'

    # 写入记录
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO checkins (member_id, check_date, check_type, content, photo_path)
        VALUES (?, ?, ?, ?, ?)
    ''', (member_id, today, check_type, content, photo_path))
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
    today = datetime.now().strftime('%Y-%m-%d')
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


@app.route('/leaderboard')
def leaderboard():
    """公开榜单"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT id, name FROM members WHERE is_admin = 0 AND is_active = 1
    ''')
    members = [dict(r) for r in cur.fetchall()]

    # 计算累计分
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
        rows.append({'name': m['name'], 'total': total})

    rows.sort(key=lambda x: -x['total'])
    conn.close()

    return render_template('leaderboard.html', rows=rows)


@app.route('/admin')
def admin_dashboard():
    """管理员仪表盘"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))

    conn = get_db()
    cur = conn.cursor()

    # 概览数据
    cur.execute('SELECT COUNT(*) FROM members WHERE is_admin = 0 AND is_active = 1')
    total_members = cur.fetchone()[0]

    today = datetime.now().strftime('%Y-%m-%d')
    cur.execute('SELECT COUNT(DISTINCT member_id) FROM checkins WHERE check_date = ? AND is_valid = 1', (today,))
    today_active = cur.fetchone()[0]

    cur.execute('SELECT COUNT(*) FROM checkins WHERE check_date = ? AND check_type = ?', (today, 'morning'))
    morning_count = cur.fetchone()[0]

    cur.execute('SELECT COUNT(*) FROM checkins WHERE check_date = ? AND check_type = ?', (today, 'evening'))
    evening_count = cur.fetchone()[0]

    # 全部成员 + 累计分
    cur.execute('SELECT id, name, joined_at FROM members WHERE is_admin = 0 AND is_active = 1 ORDER BY id')
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

    conn.close()
    return render_template('admin.html',
                           name=session['member_name'],
                           total_members=total_members,
                           today_active=today_active,
                           morning_count=morning_count,
                           evening_count=evening_count,
                           rows=rows,
                           today=today)


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

    conn.close()
    return render_template('member_detail.html', member=member, records=records)


@app.route('/admin/leaderboard_image')
def admin_leaderboard_image():
    """生成并下载榜单图"""
    if not session.get('is_admin'):
        return redirect(url_for('index'))

    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id, name FROM members WHERE is_admin = 0 AND is_active = 1')
    members = [dict(r) for r in cur.fetchall()]

    today = datetime.now().strftime('%Y-%m-%d')
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
                     download_name=f'leaderboard_{datetime.now().strftime("%Y%m%d")}.png')


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
    today = datetime.now().strftime('%Y-%m-%d')
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
    today = datetime.now().strftime('%Y-%m-%d')
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