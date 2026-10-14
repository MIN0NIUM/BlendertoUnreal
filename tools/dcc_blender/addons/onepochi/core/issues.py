"""バリデーション結果の表現（docs/05_バリデーション規則.md）。

重大度:
    error   ... 書き出しを中断する
    warning ... 続行するがマニフェストと UE 側レポートに残す
    info    ... 記録のみ
"""

ERROR = "error"
WARNING = "warning"
INFO = "info"

SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}

SEVERITY_LABEL = {ERROR: "エラー", WARNING: "警告", INFO: "情報"}
SEVERITY_ICON = {ERROR: "ERROR", WARNING: "ERROR_OUTLINE", INFO: "INFO"}


class Issue(object):
    """1件の検証結果。"""

    __slots__ = ("rule_id", "severity", "target", "message", "auto_fixed")

    def __init__(self, rule_id, severity, message, target=None, auto_fixed=False):
        self.rule_id = rule_id
        self.severity = severity
        self.message = message
        self.target = target
        self.auto_fixed = auto_fixed

    def to_manifest(self):
        return {
            "ruleId": self.rule_id,
            "severity": self.severity,
            "target": self.target,
            "message": self.message,
            "autoFixed": self.auto_fixed,
        }

    def format_line(self):
        head = "[{0}] {1}".format(self.rule_id, SEVERITY_LABEL[self.severity])
        if self.target:
            return "{0}: {1} — {2}".format(head, self.target, self.message)
        return "{0}: {1}".format(head, self.message)

    def __repr__(self):
        return "<Issue {0} {1} {2}>".format(self.rule_id, self.severity, self.target)


class IssueList(object):
    """検証結果の集合。severity の集計と並び替えを担う。"""

    def __init__(self, issues=None):
        self._issues = list(issues or [])

    def __iter__(self):
        return iter(self._issues)

    def __len__(self):
        return len(self._issues)

    def __getitem__(self, index):
        return self._issues[index]

    def add(self, rule_id, severity, message, target=None, auto_fixed=False):
        issue = Issue(rule_id, severity, message, target, auto_fixed)
        self._issues.append(issue)
        return issue

    def error(self, rule_id, message, target=None):
        return self.add(rule_id, ERROR, message, target)

    def warn(self, rule_id, message, target=None, auto_fixed=False):
        return self.add(rule_id, WARNING, message, target, auto_fixed)

    def info(self, rule_id, message, target=None):
        return self.add(rule_id, INFO, message, target)

    def extend(self, other):
        self._issues.extend(other)
        return self

    def count(self, severity):
        return sum(1 for i in self._issues if i.severity == severity)

    @property
    def errors(self):
        return [i for i in self._issues if i.severity == ERROR]

    @property
    def warnings(self):
        return [i for i in self._issues if i.severity == WARNING]

    def has_error(self):
        return any(i.severity == ERROR for i in self._issues)

    def status(self):
        """マニフェストの validation.status に入れる値。"""
        if self.has_error():
            return "failed"
        if self.count(WARNING):
            return "passedWithWarnings"
        return "passed"

    def sorted(self):
        """error -> warning -> info、同じ重大度内では ruleId 順。"""
        return sorted(
            self._issues,
            key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.rule_id, i.target or ""),
        )

    def to_manifest(self):
        return [i.to_manifest() for i in self.sorted()]

    def summary(self):
        """UI 表示用の1行サマリ。"""
        return "エラー {0} 件 / 警告 {1} 件 / 情報 {2} 件".format(
            self.count(ERROR), self.count(WARNING), self.count(INFO)
        )

    def format_report(self):
        lines = [self.summary()]
        for issue in self.sorted():
            lines.append("  " + issue.format_line())
        return "\n".join(lines)
