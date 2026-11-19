"""Verify the packaged Linux server and three HTTP agent identities across a TLS restart."""
import argparse
import http.client
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
from unittest.mock import patch

from agent_group_chat.common import ident, token

URL = "https://192.0.2.12:7810"
DIST = Path(__file__).resolve().parents[1] / "dist/diesheng-linux.tar.gz"


def exercise(root, restart):
    def request(path, data=None, headers=None):
        ctx=ssl.create_default_context(cafile=root/"ca.pem")
        conn=http.client.HTTPSConnection("192.0.2.12",7810,timeout=8,context=ctx)
        try:
            conn.request("GET" if data is None else "POST",path,None if data is None else json.dumps(data),
                         {"Content-Type":"application/json",**(headers or {})})
            response=conn.getresponse()
            content=response.read()
            assert response.status==200,(response.status,content)
            return content,dict(response.getheaders())
        finally:
            conn.close()
    key=(root/"owner.key").read_text().strip()
    cookie=request("/api/owner/login",{"key":key})[1]["Set-Cookie"].split(";")[0]
    def owner(op,**fields):
        return json.loads(request("/api/owner/"+op,fields,{"Cookie":cookie})[0])
    def agent(member,op,**fields):
        return json.loads(request("/api/project/"+op,{"session":member["agent"],**fields},{"Authorization":"Bearer "+member["key"]})[0])
    assert not owner("groups")["groups"]
    gid=owner("create",name="服务器验收")["group"]
    link=owner("channel_link",group=gid)["url"]
    assert link.startswith(URL+"/join#c=")
    assert b"/api/channel/join" in request("/join")[0]
    assert b"join.js" in request("/join",headers={"Accept":"text/html"})[0]
    assert b"/api/project/context" in request("/project-memory.md")[0]
    for icon in ("codex.webp","claude-code.webp","deepseek-harness.webp"):
        body,headers=request("/"+icon)
        assert headers["Content-Type"]=="image/webp" and body[:4]==b"RIFF" and body[8:12]==b"WEBP"
    assert b"channel-filter" in request("/")[0]
    guests=[]
    for kind in ("codex","claude","deepseek"):
        secret=token()
        info=json.loads(request("/api/channel/join",{"code":link.split("#c=")[1],"agent":kind},
                                {"Authorization":"Bearer "+secret})[0])
        guest={**info,"key":secret,"agent":kind}
        agent(guest,"check",challenge=agent(guest,"status")["challenge"])
        guests.append(guest)
    previous=(root/"ca.pem").read_bytes()
    restart()
    assert (root/"ca.pem").read_bytes()==previous and (root/"owner.key").read_text().strip()==key
    assert owner("channel_link",group=gid)["url"]==link
    for index,sender in enumerate(guests):
        recipient=guests[(index+1)%len(guests)]
        msg=agent(sender,"post",kind="question",target=recipient["member"],body="可以联调吗？",request=ident())
        received=agent(recipient,"wait",timeout=0)["delivery"]
        assert received["id"]==msg["id"]
        ans=agent(recipient,"post",kind="answer",body="可以。",parent=msg["id"],ticket=received["ticket"],request=ident())
        assert agent(sender,"wait",timeout=0)["delivery"]["id"]==ans["id"]
        agent(sender,"accept",answer=ans["id"])
    assert len(owner("conclusions",group=gid)["messages"])==3


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",default="python:3.12-slim-bookworm")
    args=parser.parse_args()
    def docker(*argv):
        return subprocess.check_output(["docker",*map(str,argv)],text=True).strip()
    cid=docker("create","--user","65534:65534","--cap-drop=ALL","--security-opt=no-new-privileges",
               "-p","127.0.0.1::7810","--workdir","/tmp","--entrypoint","/bin/sh",args.image,"-ec",
               "tar -xzf /tmp/linux.tar.gz && exec python3 -I agent-group-chat-linux/setup.py "
               +URL+" --data-dir /tmp/relay-state --foreground")
    try:
        docker("cp",DIST,cid+":/tmp/linux.tar.gz")
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            port=0
            direct=socket.create_connection
            def route(addr,*args,**kwargs):
                assert addr==("192.0.2.12",7810)
                return direct(("127.0.0.1",port),*args,**kwargs)
            def start():
                nonlocal port
                docker("start",cid)
                port=int(docker("port",cid,"7810/tcp").split(":")[-1])
                for _ in range(80):
                    if docker("inspect","-f","{{.State.Running}}",cid)!="true":
                        raise RuntimeError(docker("logs",cid))
                    if "叠声已启动" in docker("logs",cid): break
                    time.sleep(.2)
                docker("cp",cid+":/tmp/relay-state/tls/ca.pem",root/"ca.pem")
                docker("cp",cid+":/tmp/relay-state/owner.key",root/"owner.key")
                for _ in range(30):
                    c=http.client.HTTPSConnection("192.0.2.12",7810,timeout=2,context=ssl.create_default_context(cafile=root/"ca.pem"))
                    try:
                        c.request("GET","/health")
                        assert json.load(c.getresponse())=={"status":"ok"}
                        return
                    except OSError: time.sleep(.25)
                    finally: c.close()
                raise RuntimeError("Linux HTTPS 健康检查未通过")
            def restart():
                docker("stop","--time","3",cid)
                start()
            with patch.object(socket,"create_connection",route):
                start()
                exercise(root,restart)
            print("PASS · Linux 部署包首次启动空工作区，重启保留密钥、邀请和成员；三个 Agent 身份完成 HTTPS 问答与采纳。")
    finally:
        docker("rm","--force",cid)


if __name__=="__main__":
    main()
