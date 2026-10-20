import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync

TASKS = ["1.1", "1.2", "1.3"]


def iss(n, login, nick, group, task, status, upd="2026-10-20T10:00:00Z"):
    body = "### Ник\n\n%s\n\n### Группа\n\n%s\n\n### Задание\n\n%s — x\n\n### Код решения\n\n```python\nprint(1)\n```" % (nick, group, task)
    return {"number": n, "user": {"login": login}, "body": body, "labels": [{"name": "сдача"}] + ([{"name": status}] if status else []), "created_at": upd, "updated_at": upd, "html_url": "https://github.com/x/y/issues/%d" % n}


class T(unittest.TestCase):
    def build(self, issues, students=None):
        subs = [s for s in (sync.parse_issue(x) for x in issues) if s]
        return sync.build_files(subs, students if students is not None else {}, TASKS)

    def test_groups_split(self):
        f, stu = self.build([iss(1, "anna", "an", "9А", "1.1", "решено"), iss(2, "boris", "bo", "9Б", "1.1", "ошибки"), iss(3, "vera", "ve", "", "1.2", "решено")])
        self.assertIn("groups/9А/ведомость.csv", f); self.assertIn("groups/9Б/ведомость.csv", f); self.assertIn("groups/_без_группы/ведомость.csv", f)
        self.assertIn("vera", f["groups/_без_группы/ведомость.csv"]); self.assertNotIn("vera", f["groups/9А/ведомость.csv"])

    def test_best_status_and_counts(self):
        f, _ = self.build([iss(1, "anna", "an", "9А", "1.1", "ошибки"), iss(2, "anna", "an", "9А", "1.1", "решено", "2026-10-21T10:00:00Z"), iss(3, "anna", "an", "9А", "1.2", "ошибки")])
        row = f["groups/9А/ведомость.csv"].split("\r\n")[1].split(";")
        self.assertEqual(row[4:7], ["решено", "ошибки", ""]); self.assertEqual(row[7], "1"); self.assertEqual(row[9], "2")

    def test_students_csv_autoadd_and_override(self):
        st = sync.read_students("github;фио;группа;примечание\nanna;Иванова Анна;10В;\n")
        f, stu = self.build([iss(1, "Anna", "an", "9А", "1.1", "решено"), iss(2, "new", "n", "9А", "1.1", "решено")], st)
        self.assertIn("groups/10В/ведомость.csv", f); self.assertIn("Иванова Анна", f["groups/10В/ведомость.csv"])
        self.assertIn("new;;9А;", stu); self.assertIn("anna;Иванова Анна;10В", stu)

    def test_no_response_group(self):
        f, _ = self.build([iss(1, "x", "x", "_No response_", "1.1", "решено")])
        self.assertIn("groups/_без_группы/ведомость.csv", f)

    def test_path_traversal_group(self):
        for bad in ["../../.github/workflows", "..\\x", "a/b", "9А/../../etc", "\u202e9", "<script>"]:
            g = sync.norm_group(bad); self.assertNotIn("/", g); self.assertNotIn("..", g); self.assertNotIn("\\", g)
        f, _ = self.build([iss(1, "x", "x", "../../evil", "1.1", "решено")])
        self.assertTrue(all(not p.startswith("..") and "/../" not in p for p in f))

    def test_csv_and_markdown_injection(self):
        f, _ = self.build([iss(1, "x", '=HYPERLINK("http://e")', "9А", "1.1", "решено"), iss(2, "y", "<img src=x onerror=1>|", "9А", "1.1", "решено")], sync.read_students("github;фио;группа;примечание\nx;=cmd|' /C calc'!A0;;\n"))
        csvt = f["groups/9А/ведомость.csv"]
        self.assertNotIn(";=", csvt); self.assertNotIn(';"=', csvt)
        md = f["groups/9А/README.md"]; self.assertNotIn("<img", md)

    def test_issue_without_task_ignored(self):
        self.assertIsNone(sync.parse_issue({"body": "### Ник\n\nx", "labels": []}))

    def test_idempotent(self):
        iss1 = [iss(1, "anna", "an", "9А", "1.1", "решено")]
        a = self.build(iss1)[0]; b = self.build(iss1)[0]
        self.assertEqual(a, b)

    def test_cli_refuses_public_and_writes_private_flag(self):
        d = tempfile.mkdtemp(); json.dump([iss(1, "a", "a", "9А", "1.1", "решено")], open(d + "/i.json", "w")); json.dump([{"id": t} for t in TASKS], open(d + "/t.json", "w"))
        import subprocess
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "sync.py"), "--issues", d + "/i.json", "--tasks", d + "/t.json", "--out", d], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertNotEqual(r.returncode, 0); self.assertFalse(os.path.exists(d + "/all.csv"))
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "sync.py"), "--issues", d + "/i.json", "--tasks", d + "/t.json", "--out", d, "--allow-public"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertTrue(os.path.exists(d + "/groups/9А/ведомость.csv"))

    def test_large(self):
        import random
        iss_l = [iss(i, "u%d" % (i % 300), "n", random.choice(["9А", "9Б", "10В", ""]), "1.%d" % (1 + i % 3), random.choice(["решено", "ошибки", None])) for i in range(1, 5001)]
        f, _ = self.build(iss_l); self.assertGreater(len(f), 4)


if __name__ == "__main__":
    unittest.main()
