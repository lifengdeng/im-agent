from pathlib import Path
import subprocess
import sys
import unittest


class OfflineNetworkTests(unittest.TestCase):
    def test_explicit_business_https_does_not_enable_feishu_or_transport_egress(self):
        program = '''
import socket
from im_agent.offline_network import install
try:
    install("transport", external_https=["api.deepseek.com"])
except ValueError:
    pass
else:
    raise AssertionError("transport cannot receive external permission")
install("worker", external_https=["api.deepseek.com"])
for host in ["open.feishu.cn", "example.com"]:
    try:
        socket.getaddrinfo(host, 443)
    except PermissionError:
        pass
    else:
        raise AssertionError("unlisted endpoint was resolved")
'''
        result = subprocess.run([sys.executable, "-B", "-c", program], cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_fixed_service_alias_tracks_replacement_ip_without_expanding_egress(self):
        program = '''
from im_agent.offline_network import install
import socket,sys
current=["192.0.2.1"]
def resolve(host,port,**kwargs):
    assert (host,port)==("transport",8090)
    return [(socket.AF_INET,socket.SOCK_STREAM,6,"",(current[0],port))]
socket.getaddrinfo=resolve
install("worker",business_proxy=True)
sys.audit("socket.connect",None,("192.0.2.1",8090))
current[0]="192.0.2.2"
sys.audit("socket.connect",None,("192.0.2.2",8090))
for address in [("192.0.2.1",8090),("192.0.2.2",443)]:
    try: sys.audit("socket.connect",None,address)
    except PermissionError: pass
    else: raise AssertionError("unregistered destination accepted")
'''
        result = subprocess.run([sys.executable, "-B", "-c", program], cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_dns_tcp_and_udp_are_blocked_before_external_io(self):
        program = '''
from im_agent.offline_network import install
import socket
install("worker")
blocked = 0
for action in [lambda: socket.getaddrinfo("example.invalid",443),
               lambda: socket.socket().connect(("192.0.2.1",443)),
               lambda: socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto(b"fixture",("192.0.2.1",53))]:
    try: action()
    except PermissionError: blocked += 1
assert blocked == 3
'''
        result = subprocess.run([sys.executable, "-B", "-c", program], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_worker_can_still_use_loopback_api(self):
        program = '''
from im_agent.offline_network import install
from im_agent.ledger import Worker
from im_agent.service import server,Client
from pathlib import Path
import tempfile,threading
install("worker")
with tempfile.TemporaryDirectory() as tmp:
    tokens={"transport":"t"*40,"executor":"e"*40}
    http=server(("127.0.0.1",0),Worker(Path(tmp)/"worker.sqlite3"),tokens)
    threading.Thread(target=http.serve_forever,daemon=True).start()
    try:
        assert Client("http://127.0.0.1:"+str(http.server_port),tokens["executor"]).call("/v1/status",{})["production_enabled"] is False
    finally:
        http.shutdown();http.server_close()
'''
        result = subprocess.run([sys.executable, "-B", "-c", program], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
