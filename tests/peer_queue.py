#!/usr/bin/env python3
"""Deterministic model decisions at the native Codex queue boundary of the integration test."""
import json
import os
import shlex
import subprocess
import sys


def run():
    if "--help" in sys.argv:
        return
    notice = sys.argv[sys.argv.index("--message") + 1]
    read = shlex.split(next(line for line in notice.splitlines() if " --read " in line))
    event = json.loads(subprocess.check_output(read))
    command, mid = read[:-2], read[-1]
    if os.environ["TEST_PEER"] == "analysis":
        if event["kind"] == "question":
            peers = json.loads(subprocess.check_output([*command,"--members"]))
            target = next(p["id"] for p in peers if p["name"] == "后端")
            args, body = ["--ask",mid,"--to",target], "请核对 v13 的验收报告"
        elif "v12" in event["body"]:
            args, body = ["--followup",mid], "请提供 v13 结果"
        else:
            args, body = ["--reply",mid], "已核对 v13，可以发布"
    else:
        args = ["--reply",mid]
        body = "手头是 v12 的报告" if event["kind"] == "question" else "v13 已通过"
    subprocess.run([*command,*args],input=body,text=True,check=True)


if __name__ == "__main__":
    run()
