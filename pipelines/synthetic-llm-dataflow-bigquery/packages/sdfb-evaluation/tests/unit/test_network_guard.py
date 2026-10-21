#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""The package-wide network guard (`tests/netguard.py`, installed by
`tests/conftest.py`): a test that is not marked `gcp` cannot open a
connection off this machine, resolve a host name or look for Google
credentials; loopback and unix sockets work.

Design: docs/designs/2026-07-07-evaluation-framework-design.md
"""

from __future__ import annotations

import socket

import google.auth
import pytest
from netguard import NetworkGuard

# TEST-NET-1 (RFC 5737): reserved for documentation, never routed
_ELSEWHERE = ("192.0.2.1", 443)


def test_a_connection_off_this_machine_fails_the_test(no_network):
  with pytest.raises(pytest.fail.Exception, match="network"):
    socket.create_connection(_ELSEWHERE, timeout=0.1)
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    with pytest.raises(pytest.fail.Exception, match=r"192\.0\.2\.1"):
      probe.connect(_ELSEWHERE)
    with pytest.raises(pytest.fail.Exception, match="network"):
      probe.connect_ex(_ELSEWHERE)
  with pytest.raises(pytest.fail.Exception, match=r"bigquery\.googleapis\.com"):
    socket.getaddrinfo("bigquery.googleapis.com", 443)
  assert len(no_network) == 4
  no_network.clear()  # seen and asserted: this test itself passes


def test_looking_for_google_credentials_fails_the_test(no_network):
  with pytest.raises(pytest.fail.Exception, match=r"google\.auth\.default"):
    google.auth.default()
  assert no_network == ["google.auth.default()"]
  no_network.clear()


def test_resolving_a_name_off_this_machine_fails_the_test(no_network):
  """The older resolvers leave the machine as `getaddrinfo` does."""
  for resolve in (socket.gethostbyname, socket.gethostbyname_ex):
    with pytest.raises(pytest.fail.Exception, match=r"example\.com"):
      resolve("example.com")
  with pytest.raises(pytest.fail.Exception, match=r"192\.0\.2\.1"):
    socket.gethostbyaddr(_ELSEWHERE[0])
  assert no_network == [
      "gethostbyname('example.com')", "gethostbyname_ex('example.com')",
      "gethostbyaddr('192.0.2.1')"
  ]
  no_network.clear()
  # this machine still answers for itself
  assert socket.gethostbyname("127.0.0.1") == "127.0.0.1"
  assert socket.gethostbyname("localhost")
  assert socket.gethostbyname_ex("localhost")[2]
  assert socket.gethostbyaddr("127.0.0.1")[0]
  assert not no_network


def test_a_datagram_off_this_machine_fails_the_test(no_network):
  """UDP needs no connection: `sendto` and `sendmsg` name the address
  themselves."""
  with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
    with pytest.raises(pytest.fail.Exception, match=r"sendto.*192\.0\.2\.1"):
      probe.sendto(b"x", _ELSEWHERE)
    with pytest.raises(pytest.fail.Exception, match=r"sendto.*192\.0\.2\.1"):
      probe.sendto(b"x", 0, _ELSEWHERE)  # the form with flags
    with pytest.raises(pytest.fail.Exception, match=r"sendmsg.*192\.0\.2\.1"):
      probe.sendmsg([b"x"], [], 0, _ELSEWHERE)
  assert len(no_network) == 3
  no_network.clear()
  with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5)
    here = receiver.getsockname()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
      sender.sendto(b"one", here)
      assert receiver.recv(16) == b"one"
      sender.sendto(b"two", 0, here)
      assert receiver.recv(16) == b"two"
      sender.sendmsg([b"three"], [], 0, here)
      assert receiver.recv(16) == b"three"
      # connected (to loopback): no address goes with the datagram
      sender.connect(here)
      sender.sendmsg([b"four"])
      assert receiver.recv(16) == b"four"
  assert not no_network


def test_loopback_and_unix_sockets_stay_open(no_network, tmp_path, monkeypatch):
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with socket.create_connection(server.getsockname(), timeout=5) as client:
      assert client.getpeername() == server.getsockname()
  assert socket.getaddrinfo("localhost", 0)
  # a unix socket path is short by law (about 100 bytes) and a temporary
  # directory's need not be: the socket is named from inside it
  monkeypatch.chdir(tmp_path)
  with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
    server.bind("guard.sock")
    server.listen(1)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
      client.connect("guard.sock")
  assert not no_network


def test_a_swallowed_attempt_still_fails_when_the_test_ends(no_network):
  """Code that catches everything cannot hide an attempt: the guard
  fails the test when it ends."""
  inner = NetworkGuard()
  inner.install()
  try:
    socket.getaddrinfo("example.com", 443)
  except BaseException:  # pylint: disable=broad-exception-caught  # the point of the test: a swallowed attempt
    pass
  finally:
    inner.remove()
  assert inner.attempts == ["getaddrinfo('example.com')"]
  with pytest.raises(pytest.fail.Exception, match=r"example\.com"):
    inner.verify()
  assert not inner.attempts
  inner.verify()  # nothing left: it passes
  assert not no_network  # the inner guard stopped it before the session's
