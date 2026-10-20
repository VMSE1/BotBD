#!/usr/bin/env python3
"""Робот-ведомость. Забирает сдачи из публичного репозитория курса и раскладывает их по группам.

Запуск на GitHub делает workflow .github/workflows/sync.yml (раз в час и по кнопке).
Локально (для проверки):  python bot/sync.py --issues issues.json --tasks tasks.json --out /tmp/out --allow-public

Что делает:
  1. Читает Issues с меткой «сдача» из SRC_REPO (по умолчанию VMSE1/python).
  2. Берёт GitHub-аккаунт автора, ник, номер группы, задание и статус.
  3. Сопоставляет аккаунт с ФИО и группой из students.csv (его правишь ты; новые аккаунты робот дописывает сам).
  4. Пишет groups/<группа>/ведомость.csv и README.md, all.csv, data/submissions.csv. Без группы: groups/_без_группы/.

Безопасность: если репозиторий с роботом НЕ приватный, данные не пишутся (ФИО детей не должны быть публичными).
Работает на одном Python 3.8+, без сторонних библиотек.
"""
import argparse, csv, io, json, os, re, sys, unicodedata, urllib.request

NO_GROUP = "_без_группы"
STATUS_RANK = {"решено": 3, "ошибки": 2, "ждёт": 1}
STUDENT_COLS = ["github", "фио", "группа", "примечание"]


# ---------- разбор данных ----------
def sections(body):
    out = {}
    for chunk in re.split(r"^### ", body or "", flags=re.M)[1:]:
        head, _, rest = chunk.partition("\n")
        out[head.strip()] = rest.strip()
    return out


def clean_text(s, n=60):
    s = unicodedata.normalize("NFKC", str(s or ""))
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:n]


def norm_group(raw):
    """Группа как безопасное имя папки: буквы, цифры, - и _. Пусто -> _без_группы. Защита от ../ и т. п."""
    g = unicodedata.normalize("NFKC", str(raw or "")).upper()
    g = re.sub(r"[^0-9A-ZА-ЯЁ_-]", "", g)[:20].strip("-_")
    return NO_GROUP if g in ("", "НЕТ", "НЕТ_ГРУППЫ", "0", "НЕТУ") else g


# ---------- «честный ввод»: те же правила, что в docs/honest.js курса ----------
def hn_norm(t):
    t = str(t or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(l.rstrip() for l in t.split("\n")).strip()


def hn_hash(t):
    h = 0x811C9DC5
    for byte in hn_norm(t).encode("utf-8"):
        h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
    return "%08x" % h


def hn_parse(s):
    s = (s or "").strip()
    if not s.startswith("v1;"):
        return None
    m = {}
    for part in s.split(";")[1:]:
        k, _, v = part.partition("=")
        if k == "h":
            m["h"] = re.sub(r"[^0-9a-f]", "", v.lower())[:8]
        elif k in ("t", "p", "b", "e", "s"):
            m[k] = int(v) if v.isdigit() and len(v) < 9 else 0
    return m


def hn_verdict(m, code):
    """(уровень, причины): ok / warn / none."""
    if m is None:
        return "none", ["отправлено не через сайт"]
    r, n = [], len(hn_norm(code))
    if m.get("h") and m["h"] != hn_hash(code):
        r.append("код изменён после сайта")
    if m.get("p", 0) > 0:
        r.append("пытался вставить: %d" % m["p"])
    if m.get("b", 0) > 0:
        r.append("вставка кусками: %d" % m["b"])
    if m.get("e", 0) > 0:
        r.append("код изменён скриптом")
    if n >= 60 and m.get("t", 0) < n * 0.4:
        r.append("набрано %d из %d символов" % (m.get("t", 0), n))
    if m.get("s", 0) > 0 and m.get("t", 0) / m["s"] > 12:
        r.append("слишком быстрый набор")
    return ("warn" if r else "ok"), r


def code_of(sec):
    m = re.match(r"^```[^\n]*\n(.*?)\n?```\s*$", (sec or "").strip(), re.S)
    return m.group(1) if m else (sec or "")


def status_of(labels):
    names = [l.get("name") if isinstance(l, dict) else l for l in labels or []]
    return "решено" if "решено" in names else ("ошибки" if "ошибки" in names else "ждёт")


def parse_issue(x):
    s = sections(x.get("body"))
    m = re.match(r"(\d+\.\d+)", s.get("Задание", ""))
    if not m:
        return None
    login = (x.get("user") or {}).get("login") or "unknown"
    level, why = hn_verdict(hn_parse(s.get("Служебное (не меняй)")), code_of(s.get("Код решения")))
    return {"num": x.get("number"), "login": login, "nick": clean_text(s.get("Ник")) or "-", "group": norm_group(s.get("Группа") if s.get("Группа") not in (None, "_No response_") else ""),
            "task": m.group(1), "status": status_of(x.get("labels")), "created": (x.get("created_at") or "")[:16].replace("T", " "),
            "updated": (x.get("updated_at") or "")[:16].replace("T", " "), "url": x.get("html_url") or "",
            "check": level, "why": "; ".join(why)}


# ---------- students.csv ----------
def read_students(text):
    rows = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"):
        k = (r.get("github") or "").strip().lower()
        if k:
            rows[k] = {c: (r.get(c) or "").strip() for c in STUDENT_COLS}
    return rows


def write_students(rows):
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\n")
    w.writerow(STUDENT_COLS)
    for k in sorted(rows):
        r = rows[k]
        w.writerow([safe(r["github"]), safe(r["фио"]), r["группа"], safe(r["примечание"])])
    return out.getvalue()


def safe(v):
    """Защита от формул в Excel: ячейка, начинающаяся на = + - @, получает апостроф."""
    v = str(v if v is not None else "")
    return "'" + v if re.match(r"^[=+\-@\t\r]", v) else v


# ---------- агрегирование ----------
def aggregate(subs, students, task_ids):
    by = {}
    for s in sorted(subs, key=lambda x: (x["updated"], x["num"] or 0)):
        k = s["login"].lower()
        u = by.setdefault(k, {"login": s["login"], "nicks": [], "form_group": "", "tasks": {}, "attempts": 0, "fails": 0, "warn": 0, "first": s["created"], "last": ""})
        if s["nick"] not in u["nicks"]:
            u["nicks"].append(s["nick"])
        if s["group"] != NO_GROUP:
            u["form_group"] = s["group"]
        cur = u["tasks"].get(s["task"])
        if cur is None or STATUS_RANK[s["status"]] >= STATUS_RANK[cur]:
            u["tasks"][s["task"]] = s["status"]
        u["attempts"] += 1
        u["fails"] += s["status"] == "ошибки"
        u["warn"] += s["check"] == "warn"
        u["first"] = min(u["first"], s["created"]) if u["first"] else s["created"]
        u["last"] = max(u["last"], s["updated"])
    # дописываем в students.csv новые аккаунты (существующие строки не трогаем)
    changed = False
    for k, u in by.items():
        if k not in students:
            students[k] = {"github": u["login"], "фио": "", "группа": u["form_group"] if u["form_group"] != NO_GROUP else "", "примечание": ""}
            changed = True
    for k, u in by.items():
        st = students[k]
        u["fio"] = st["фио"]
        u["group"] = norm_group(st["группа"]) if st["группа"] else (u["form_group"] or NO_GROUP)
        u["note"] = st["примечание"]
        u["solved"] = sum(1 for t in task_ids if u["tasks"].get(t) == "решено")
    return by, changed


def table_rows(users, task_ids):
    head = ["Группа", "ФИО", "GitHub", "Ник"] + task_ids + ["Решено", "Всего заданий", "Ошибочных попыток", "Сдач с подозрением на вставку", "Первая сдача", "Последняя активность", "Профиль"]
    rows = [head]
    for u in sorted(users, key=lambda u: (u["group"], (u["fio"] or "я").lower(), u["login"].lower())):
        rows.append([u["group"], safe(u["fio"]), safe(u["login"]), safe(", ".join(u["nicks"]))] + [u["tasks"].get(t, "") for t in task_ids] +
                    [u["solved"], len(task_ids), u["fails"], u["warn"], u["first"], u["last"], "https://github.com/" + re.sub(r"[^0-9A-Za-z-]", "", u["login"])])
    return rows


def csv_text(rows):
    out = io.StringIO()
    csv.writer(out, delimiter=";", lineterminator="\r\n").writerows(rows)
    return "\ufeff" + out.getvalue()


def md_cell(v):
    return str(v).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def md_table(rows, task_ids):
    sym = {"решено": "✅", "ошибки": "❌", "ждёт": "⏳", "": ""}
    head = ["ФИО", "GitHub"] + task_ids + ["Решено"]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * len(head)) + "|"]
    for r in rows[1:]:
        tasks = r[4:4 + len(task_ids)]
        lines.append("| " + " | ".join([md_cell(r[1] or "—"), md_cell(r[2])] + [sym.get(t, "") for t in tasks] + ["**%s**/%s" % (r[4 + len(task_ids)], len(task_ids))]) + " |")
    return "\n".join(lines)


def build_files(subs, students, task_ids):
    """Возвращает (файлы {путь: текст}, новый students.csv)."""
    by, _ = aggregate(subs, students, task_ids)
    users = list(by.values())
    files = {}
    groups = {}
    for u in users:
        groups.setdefault(u["group"], []).append(u)
    allrows = table_rows(users, task_ids)
    files["all.csv"] = csv_text(allrows)
    for g, us in sorted(groups.items()):
        rows = table_rows(us, task_ids)
        files["groups/%s/ведомость.csv" % g] = csv_text(rows)
        solved = sum(u["solved"] for u in us)
        files["groups/%s/README.md" % g] = "# Группа %s\n\nУчеников: %d, решено заданий: %d из %d\n\n%s\n\n✅ решено, ❌ есть ошибки, ⏳ ждёт проверки. Файл `ведомость.csv` открывается в Excel.\n" % (
            md_cell(g), len(us), solved, len(us) * len(task_ids), md_table(rows, task_ids))
    sub_rows = [["Дата", "GitHub", "Ник", "Группа", "Задание", "Статус", "Как набран код", "Ссылка"]] + [
        [s["updated"], safe(s["login"]), safe(s["nick"]), s["group"], s["task"], s["status"], ("набран сам" if s["check"] == "ok" else safe(s["why"])), s["url"]] for s in sorted(subs, key=lambda x: (x["updated"], x["num"] or 0))]
    files["data/submissions.csv"] = csv_text(sub_rows)
    idx = ["# Ведомость курса Python", "", "Данные приватные. Обновляется роботом раз в час (Actions → «Обновить ведомость» → Run workflow для обновления сейчас).", "",
           "| Группа | Учеников | Решено заданий |", "|---|---|---|"]
    for g, us in sorted(groups.items()):
        idx.append("| [%s](groups/%s/README.md) | %d | %d |" % (md_cell(g), g, len(us), sum(u["solved"] for u in us)))
    idx += ["", "Все ученики одним файлом: [all.csv](all.csv). Журнал сдач: [data/submissions.csv](data/submissions.csv). Список учеников и групп (правь ты): [students.csv](students.csv).", ""]
    files["README.md"] = "\n".join(idx)
    return files, write_students(students)


# ---------- сеть ----------
def http_json(url, token=None):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "botbd"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_issues(repo, token):
    out, page = [], 1
    while page <= 30:
        a = http_json("https://api.github.com/repos/%s/issues?state=all&per_page=100&page=%d&labels=%s" % (repo, page, urllib.request.quote("сдача")), token)
        out += [x for x in a if "pull_request" not in x]
        if len(a) < 100:
            break
        page += 1
    return out


def fetch_tasks(repo):
    req = urllib.request.Request("https://raw.githubusercontent.com/%s/main/docs/data/tasks.js" % repo, headers={"User-Agent": "botbd"})
    with urllib.request.urlopen(req, timeout=30) as r:
        t = r.read().decode("utf-8")
    return json.loads(t[t.index("["):t.rindex("]") + 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issues"); ap.add_argument("--tasks"); ap.add_argument("--out", default="."); ap.add_argument("--allow-public", action="store_true")
    a = ap.parse_args()
    src = os.environ.get("SRC_REPO", "VMSE1/python"); token = os.environ.get("GITHUB_TOKEN")
    me = os.environ.get("GITHUB_REPOSITORY")
    if not a.allow_public:  # закрытый по умолчанию: без проверки не пишем ничего
        try:
            info = http_json("https://api.github.com/repos/%s" % me, token) if me else {}
        except Exception as e:
            sys.exit("Не удалось проверить, что репозиторий приватный: %s. Данные не записаны." % e)
        if info.get("private") is not True:
            sys.exit("ОСТАНОВЛЕНО: репозиторий %s не приватный. Сделай его Private (Settings → General → Danger Zone) и запусти заново. Данные не записаны." % me)
    raw = json.load(open(a.issues, encoding="utf-8")) if a.issues else fetch_issues(src, token)
    tasks = json.load(open(a.tasks, encoding="utf-8")) if a.tasks else fetch_tasks(src)
    task_ids = [t["id"] for t in tasks]
    subs = [s for s in (parse_issue(x) for x in raw) if s]
    sp = os.path.join(a.out, "students.csv")
    students = read_students(open(sp, encoding="utf-8").read()) if os.path.exists(sp) else {}
    files, stu = build_files(subs, students, task_ids)
    files["students.csv"] = stu
    # удаляем устаревшие папки групп (если группа исчезла), остальное перезаписываем
    for path, text in files.items():
        full = os.path.join(a.out, path)
        if os.path.realpath(full).startswith(os.path.realpath(a.out) + os.sep):
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(text)
    print("Сдач: %d, учеников: %d, групп: %d" % (len(subs), len(students), len({p.split('/')[1] for p in files if p.startswith('groups/')})))


if __name__ == "__main__":
    main()
