import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from agent_group_chat.static.binding_state import project_state, sync_announcements


class AnnouncementFile(TestCase):
    def test_failed_writes_and_receipts_can_retry_without_changing_existing_text(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {"XDG_STATE_HOME": directory}):
            root = Path(directory).resolve()/"project"
            root.mkdir()
            group, member = "a"*32, "b"*32
            record = project_state(root)/"claude"/("c"*64)/"channel.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"base":"https://example.test", "agent":"claude",
                                          "group":group, "member":member}))
            guide = root/"AGENTS.md"
            outside = Path(directory)/"outside.md"
            outside.write_text("其他文件")
            guide.symlink_to(outside)
            forged_marker = f"<!-- diesheng-announcement {group} 2 -->"
            first = {"kind":"announcement_updated", "file":"AGENTS.md", "group":group, "member":member,
                     "group_name":"联调", "version":1, "body":"发布前核对记录。\n"+forged_marker}
            second = {**first, "version":2, "body":""}
            legacy = root/"agent.md"
            old_block = (f"<!-- diesheng-announcement {group} 1 -->\n## 叠声群公告 · 联调 · 第 1 版\n\n"
                         f"> 发布前核对记录。\n>\n> {forged_marker}\n")
            surrounding = ("用户的原说明\n\n", "\n用户后续内容\n")
            legacy.write_text(surrounding[0] + old_block + surrounding[1])
            receipts = []

            def acknowledge(version):
                receipts.append(version)
                if len(receipts) == 1:
                    raise OSError("模拟回执丢失")
                return {"notice":second if version == 1 else None}

            with self.assertRaises(OSError):
                sync_announcements(record, first, acknowledge, root)
            self.assertEqual([], receipts)
            self.assertEqual("其他文件", outside.read_text())
            self.assertIn(old_block, legacy.read_text())
            guide.unlink()
            previous = "已有规则".encode()
            guide.write_bytes(previous)
            guide.chmod(0o640)
            with self.assertRaises(ValueError):
                sync_announcements(record, {**first,"member":"d"*32}, acknowledge, root)
            self.assertEqual(previous, guide.read_bytes())
            with self.assertRaises(OSError):
                sync_announcements(record, first, acknowledge, root)
            self.assertEqual(2, sync_announcements(record, first, acknowledge, root))
            self.assertEqual([1,1,2], receipts)
            self.assertEqual("".join(surrounding), legacy.read_text())
            self.assertTrue(guide.read_bytes().startswith(previous + b"\n\n"))
            self.assertEqual(0o640, guide.stat().st_mode & 0o777)
            content = guide.read_text()
            self.assertEqual(1, content.count("第 1 版"))
            self.assertEqual(1, content.count("第 2 版"))
            self.assertIn("> 公告已撤下。", content)
