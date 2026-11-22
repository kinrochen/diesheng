"""Exercise the shipped two-container topology with a test CA and loopback-only ingress."""
import http.client
import json
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tarfile
import tempfile
import time
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "dist/diesheng-linux.tar.gz"
DOMAIN = "channel.fixture.test"


def main():
    with tempfile.TemporaryDirectory() as scratch:
        with tarfile.open(PACKAGE) as archive:
            for entry in archive.getmembers():
                assert entry.isfile() and Path(entry.name).parent == Path("agent-group-chat-linux")
                archive.extract(entry, scratch)
        root = Path(scratch) / "agent-group-chat-linux"
        (root / ".env").write_text("CHAT_DOMAIN="+DOMAIN+"\n")
        project = "chat-qa-"+secrets.token_hex(5)
        def compose(*args):
            return subprocess.check_output(["docker","compose","-p",project,*args],cwd=root,text=True).strip()
        config=json.loads(compose("config","--format","json"))
        assert not config["services"]["chat"].get("ports")
        assert {p["target"] for p in config["services"]["gateway"]["ports"]}=={80,443}
        (root/"compose.override.yaml").write_text('services:\n  gateway:\n    ports: !override ["127.0.0.1::443"]\n')
        caddy=root/"Caddyfile"
        caddy.write_text(caddy.read_text().replace("    reverse_proxy", "    tls internal\n    reverse_proxy"))
        try:
            compose("up","--build","-d")
            port=int(compose("port","gateway","443").split(":")[-1])
            cert="/data/caddy/pki/authorities/local/root.crt"
            for _ in range(50):
                if subprocess.run(["docker","compose","-p",project,"exec","-T","gateway","test","-f",cert],cwd=root,
                                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
                    break
                time.sleep(.2)
            ca=compose("exec","-T","gateway","cat",cert)
            key=compose("exec","-T","chat","cat","/data/workspace/owner.key")
            direct=socket.create_connection
            def route(address,*args,**kwargs):
                assert address==(DOMAIN,port)
                return direct(("127.0.0.1",port),*args,**kwargs)
            def request(path, data=None, headers=None):
                conn=http.client.HTTPSConnection(DOMAIN,port,context=ssl.create_default_context(cadata=ca),timeout=10)
                try:
                    conn.request("GET" if data is None else "POST",path,None if data is None else json.dumps(data),
                                 {"Host":DOMAIN,"Content-Type":"application/json",**(headers or {})})
                    r=conn.getresponse()
                    return r.status,dict(r.getheaders()),r.read()
                finally:
                    conn.close()
            with patch.object(socket,"create_connection",route):
                last_error = None
                for _ in range(30):
                    try:
                        if request("/health")[0] == 200:
                            break
                    except OSError as error:
                        last_error = error
                    time.sleep(.2)
                else:
                    raise AssertionError("Caddy HTTPS 健康检查未通过") from last_error
                origin={"Origin":"https://"+DOMAIN}
                code,headers,_=request("/api/owner/login",{"key":key},origin)
                assert code==200 and "Secure" in headers["Set-Cookie"]
                browser={**origin,"Cookie":headers["Set-Cookie"].split(";")[0]}
                assert request("/api/owner/groups",{}, {**browser,"Origin":"https://other.test"})[0]==403
                assert not json.loads(request("/api/owner/groups",{},browser)[2])["groups"]
                group=json.loads(request("/api/owner/create",{"name":"部署验证"},browser)[2])["group"]
                link=json.loads(request("/api/owner/channel_link",{"group":group},browser)[2])["url"]
                assert link.startswith("https://"+DOMAIN+"/join#c=")
                assert b"/api/channel/join" in request("/join")[2]
                secret=secrets.token_hex(32)
                assert request("/api/channel/join",{"code":link.split("#c=")[1],"agent":"codex"},{"Authorization":"Bearer "+secret})[0]==200
                human_link=json.loads(request("/api/owner/human_invite",{"group":group},browser)[2])["url"]
                assert human_link.startswith("https://"+DOMAIN+"/room/"+group+"#invite=")
                form={"code":human_link.split("#invite=")[1]}
                member_path="/api/member/"+group+"/"
                status,hdrs,_=request(member_path+"preview",form,origin)
                assert status==200 and "Set-Cookie" not in hdrs
                auth={"username":"fixture.user","password":"fixture-only-password"}
                status,hdrs,raw=request(member_path+"register",{**form,**auth,"name":"值班同事"},origin)
                assert status==200 and "Secure" in hdrs["Set-Cookie"] and "Path=/api" in hdrs["Set-Cookie"]
                member=json.loads(raw)["member"]
                human={**origin,"Cookie":hdrs["Set-Cookie"].split(";")[0]}
                assert request(member_path+"detail",{},human)[0]==200 and request("/api/owner/groups",{},human)[0]==401
                status,hdrs,_=request("/api/account/login",auth,origin)
                assert status==200 and "Secure" in hdrs["Set-Cookie"]
                human={**origin,"Cookie":hdrs["Set-Cookie"].split(";")[0]}
                assert request(member_path+"detail",{},human)[0]==200
                role={"group":group,"member":member,"admin":True}
                assert request("/api/owner/admin-role",role,browser)[0]==200
                assert request(member_path+"admin-role",role,human)[0]==403
                assert request(member_path+"dissolve",{},human)[0]==403
                assert request(member_path+"announcement-publish",{"body":"发布前核对验收记录。","version":0},human)[0]==200
                agent={"Authorization":"Bearer "+secret}
                status,_,raw=request("/api/project/wait",{"session":"smoke","timeout":0,"announcements":"AGENTS.md"},agent)
                assert status==200 and json.loads(raw)["notice"]["kind"]=="announcement_updated"
                status,_,raw=request("/api/project/announcement-ack",{"session":"smoke","version":1,"file":"AGENTS.md"},agent)
                assert status==200 and json.loads(raw)["notice"] is None
                assert b"awaiting_ticket" in request("/codex-listen.py")[2]
            print("PASS · Compose 实包、Caddy HTTPS、频道管理员、Agent 入群与公告回执")
        finally:
            compose("down","--volumes","--rmi","local")


if __name__=="__main__":
    main()
