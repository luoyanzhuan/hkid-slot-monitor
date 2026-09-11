#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
香港智能身份证预约配额监控器
================================
数据来源：香港入境处官方「预约配额预览」公开接口
接口地址：GET https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getSituation?svcId=579
特点：无需登录、无需验证码、不涉及任何个人信息，只读官方公开数据。

监控粒度：每个办事处 x 每个可预约日期，分「一般服务时段 / 延长服务时段」两个配额桶，
状态为：尚有名额（绿）/ 少量名额（黄）/ 已满（红）/ 无该时段。

用法：
  python monitor.py --test    发送一条测试推送后退出
  python monitor.py --once    检查一次并退出（适合系统计划任务）
  python monitor.py           本地循环模式（默认每 10 分钟检查一次）

所有配置均通过环境变量传入，详见 README.md。
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

QUOTA_URL = ("https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/"
             "getSituation?svcId=579")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BOOKING_URL = "https://www.gov.hk/icbooking"

# 状态类 -> 中文文案
STATUS_TEXT = {
    "quota-g": "尚有名额",
    "quota-y": "少量名额",
    "quota-r": "已满",
    "no-quotaR": "无一般服务时段",
    "no-quotaK": "无延长服务时段",
}
BUCKET_TEXT = {"R": "一般服务时段", "K": "延长服务时段"}
# 兜底办事处名（正常情况下脚本会从接口返回的 office 列表自动取中文名）
FALLBACK_OFFICES = {
    "RHK": "港岛办事处(湾仔)",
    "RKO": "九龙办事处(长沙湾)",
    "RTK": "将军澳办事处",
    "FTO": "火炭办事处",
    "TMO": "屯门办事处",
    "YLO": "元朗办事处",
}

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def env(name, default=""):
    return os.environ.get(name, default)


def hk_now():
    """返回香港时区当前时间"""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz)


def parse_date(s):
    """把 '2026-09-25' 或 '09/25/2026' 统一成 'MM/DD/YYYY'；失败返回 None"""
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return None


def fetch_quota():
    """调用官方配额接口，返回 (offices, data, last_update)
    offices: {officeId: 中文名}
    data:    {officeId: {date: {"R": cls, "K": cls}}}
    """
    req = urllib.request.Request(
        QUOTA_URL,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": "https://eservices.es2.immd.gov.hk/es/quota-enquiry-client/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "data" not in payload:
        raise ValueError("接口返回结构异常: " + raw[:300])

    offices = {}
    for o in payload.get("office", []):
        chs = o.get("chs") or {}
        name = chs.get("officeName") or FALLBACK_OFFICES.get(o["officeId"], o["officeId"])
        offices[o["officeId"]] = name

    data = {}
    for row in payload.get("data", []):
        oid, date = row.get("officeId"), row.get("date")
        if not oid or not date:
            continue
        data.setdefault(oid, {})[date] = {
            "R": row.get("quotaR", ""),
            "K": row.get("quotaK", ""),
        }
    return offices, data, payload.get("lastUpdateTime", "")


def status_text(cls):
    return STATUS_TEXT.get(cls, cls or "未知")


def is_available(cls):
    return cls in ("quota-g", "quota-y")
  


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def bark_push(cfg, title, body, url):
    key = cfg["bark_key"]
    if not key:
        return False
    base = cfg["bark_url"].rstrip("/")
    # key 可以是完整 URL 或纯 key
    if key.startswith("http"):
        base = key.rstrip("/")
        key = ""
    path = f"{base}/"
    if key:
        path += key + "/"
    path += urllib.parse.quote(title) + "/" + urllib.parse.quote(body)
    if url:
        path += "?url=" + urllib.parse.quote(url, safe="")
    return _http_get(path)


def tg_push(cfg, text):
    token, chat = cfg.get("tg_token"), cfg.get("tg_chat_id")
    if not token or not chat:
        return False
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(api, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except Exception:
        return False


def webhook_push(cfg, title, body, url):
    hook = cfg.get("webhook_url")
    if not hook:
        return False
    payload = json.dumps({"title": title, "body": body, "url": url}).encode()
    req = urllib.request.Request(hook, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status < 300
    except Exception:
        return False


def notify(cfg, title, body, url=BOOKING_URL):
    ok = []
    ok.append(("Bark", bark_push(cfg, title, body, url)))
    ok.append(("Telegram", tg_push(cfg, title + "\n" + body + "\n" + url)))
    ok.append(("Webhook", webhook_push(cfg, title, body, url)))
    done = [name for name, flag in ok if flag]
    print(f"[notify] {title} | {body} | 推送通道: {','.join(done) if done else '未配置/失败'}")
    return bool(done)


def _http_get(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status < 300
    except Exception as e:
        print(f"[warn] HTTP 请求失败: {e}")
        return False
      


def build_alerts(cfg, offices, data):
    """对比 state，返回 (alerts, first_run)。alerts 为要推送的文案列表。"""
    state_path = cfg["state_file"]
    state = load_state(state_path)
    # state 结构: {"seen": {oid: {date: {"R": cls, "K": cls}}}, "alerted": {oid: {date: {bucket: ts}}}}
    seen = state.get("seen")
    first_run = not seen
    seen = seen or {}
    alerted = state.get("alerted", {})
    new_seen = {}
    alerts = []
    now = hk_now()
    min_interval = cfg["min_alert_interval_min"]

    targets = set(cfg["target_dates"])
    avail_now = []   # 首次运行时：当前可约的目标清单
    for oid, dates in data.items():
        new_seen[oid] = dates
        if oid not in cfg["target_offices"]:
            continue
        for date, buckets in dates.items():
            if targets and date not in targets:
                continue
            for bucket in ("R", "K"):
                cls = buckets.get(bucket, "")
                if not cls:
                    continue
                prev = (seen.get(oid, {}).get(date, {}) or {}).get(bucket)
                cur_avail = is_available(cls)
                prev_avail = is_available(prev) if prev else None
                is_new = prev is None
                became_available = (not prev_avail) and cur_avail
                if not cur_avail or not cfg["notify_on"].get(bucket, False):
                    continue
                if first_run:
                    office_name = offices.get(oid, oid)
                    d = datetime.datetime.strptime(date, "%m/%d/%Y")
                    weekday = WEEKDAY_CN[d.weekday()]
                    avail_now.append(f"{office_name} {date}({weekday}) {BUCKET_TEXT[bucket]}")
                elif is_new or became_available:
                    key = f"{oid}|{date}|{bucket}"
                    last = alerted.get(oid, {}).get(date, {}).get(bucket, 0)
                    if now.timestamp() - last >= min_interval * 60:
                        office_name = offices.get(oid, oid)
                        d = datetime.datetime.strptime(date, "%m/%d/%Y")
                        weekday = WEEKDAY_CN[d.weekday()]
                        alerts.append(
                            f"【{office_name}】{date}（{weekday}）{BUCKET_TEXT[bucket]}：{status_text(cls)}\n"
                            f"检测到{('新增可约日期' if is_new else '号源释出')}，请尽快登录预约。"
                        )
                        alerted.setdefault(oid, {}).setdefault(date, {})[bucket] = int(now.timestamp())
    if first_run and avail_now:
        cap = 15
        lines = avail_now[:cap]
        if len(avail_now) > cap:
            lines.append(f"……等共 {len(avail_now)} 条")
        alerts = ["监控已启动。当前以下目标已有名额（供你确认监控范围）：\n" + "\n".join(lines)]
        # 首启把已提示过的目标标记为"已提醒"，避免误判为新增
        for oid, dates in new_seen.items():
            if oid not in cfg["target_offices"]:
                continue
            for date, buckets in dates.items():
                if targets and date not in targets:
                    continue
                for bucket, cls in buckets.items():
                    if is_available(cls):
                        alerted.setdefault(oid, {}).setdefault(date, {})[bucket] = int(now.timestamp())
    save_state(state_path, {"seen": new_seen, "alerted": alerted})
    return alerts, first_run
  


def make_config():
    cfg = {
        "target_offices": [x.strip() for x in env("TARGET_OFFICES", "").split(",") if x.strip()],
        "target_dates": set(),
        "target_window_days": int(env("TARGET_WINDOW_DAYS", "30")),
        "notify_on": {"R": True, "K": True},
        "bark_key": env("BARK_KEY", ""),
        "bark_url": env("BARK_URL", "https://api.day.app"),
        "tg_token": env("TG_BOT_TOKEN", ""),
        "tg_chat_id": env("TG_CHAT_ID", ""),
        "webhook_url": env("WEBHOOK_URL", ""),
        "state_file": env("STATE_FILE", "state.json"),
        "check_interval": int(env("CHECK_INTERVAL", "600")),
        "min_alert_interval_min": int(env("MIN_ALERT_INTERVAL_MIN", "30")),
    }
    # 目标日期解析
    raw_dates = env("TARGET_DATES", "")
    if raw_dates.strip():
        for d in raw_dates.split(","):
            pd = parse_date(d)
            if pd:
                cfg["target_dates"].add(pd)
    # 通知开关
    on = env("NOTIFY_ON", "g,y").split(",")
    cfg["notify_on"] = {
        "R": ("g" in on or "y" in on or "all" in on),
        "K": ("g" in on or "y" in on or "all" in on),
    }
    return cfg


def apply_window(cfg, data):
    """未显式指定日期时，只监控未来 N 天内的日期"""
    if cfg["target_dates"]:
        return
    today = hk_now().date()
    end = today + datetime.timedelta(days=cfg["target_window_days"])
    all_dates = set()
    for dates in data.values():
        all_dates.update(dates.keys())
    for ds in all_dates:
        d = datetime.datetime.strptime(ds, "%m/%d/%Y").date()
        if today <= d <= end:
            cfg["target_dates"].add(ds)


def run_once(cfg, test=False):
    try:
        offices, data, last_update = fetch_quota()
    except Exception as e:
        print(f"[error] 配额接口获取失败: {e}")
        return 2
    apply_window(cfg, data)
    print(f"[ok] 已获取配额数据，更新于 {last_update}，"
          f"共 {len(data)} 个办事处，目标办事处: {cfg['target_offices'] or '全部'}，"
          f"目标日期数: {len(cfg['target_dates'])}")
    alerts, first_run = build_alerts(cfg, offices, data)
    if test:
        ok = notify(cfg, "预约配额监控·测试推送",
                    "这是一条测试消息。如果你的 iPhone 收到了它，说明监控通知已配置成功。")
        return 0 if ok else 1
    if alerts:
        for a in alerts:
            notify(cfg, "身份证预约名额提醒", a)
    elif first_run:
        print("[ok] 首次检查完成：当前目标暂无名额，已开始持续监控。")
    else:
        print("[ok] 本次检查：目标暂无新释出名额，不推送。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="香港身份证预约配额监控器")
    ap.add_argument("--test", action="store_true", help="发送测试推送")
    ap.add_argument("--once", action="store_true", help="只检查一次")
    args = ap.parse_args()

    cfg = make_config()
    if args.test:
        return run_once(cfg, test=True)
    if args.once or os.environ.get("CI"):
        return run_once(cfg)
    # 本地循环模式
    print(f"[ok] 本地循环模式启动，每 {cfg['check_interval']} 秒检查一次。Ctrl+C 退出。")
    while True:
        try:
            run_once(cfg)
        except Exception as e:
            print(f"[error] 检查异常: {e}")
        try:
            time.sleep(cfg["check_interval"])
        except KeyboardInterrupt:
            print("\n[ok] 已退出。")
            return 0


if __name__ == "__main__":
    sys.exit(main())
  







