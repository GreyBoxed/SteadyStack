"""What address this system calls out from.

On the box the instance itself knows: EC2's metadata service reports the
elastic IP without a request leaving AWS, so a third-party echo service being
down cannot refuse a live day. Off the box the metadata address answers
nothing, and that must cost a laptop a fraction of a second, not three.
"""

import io
import urllib.error

import pytest

from broker import network

ELASTIC_IP = "203.0.113.7"
HOME_IP = "198.51.100.42"


class FakeHTTP:
    """Answers by URL; anything it was not told about is unreachable."""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.calls: list[tuple[str, str, dict, float]] = []

    def __call__(self, request, *, timeout, direct=False):
        url = request if isinstance(request, str) else request.full_url
        method = "GET" if isinstance(request, str) else request.get_method()
        headers = {} if isinstance(request, str) else dict(request.header_items())
        self.calls.append((method, url, {k.lower(): v for k, v in headers.items()}, timeout))
        if url not in self.answers:
            raise urllib.error.URLError("no route to host")
        return io.BytesIO(self.answers[url].encode())

    def urls(self):
        return [url for _, url, _, _ in self.calls]


@pytest.fixture(autouse=True)
def no_cache():
    network.forget()
    yield
    network.forget()


def install(monkeypatch, answers):
    fake = FakeHTTP(answers)
    monkeypatch.setattr(network, "_urlopen", fake)
    return fake


def test_on_ec2_the_instance_metadata_answers_first(monkeypatch):
    fake = install(
        monkeypatch,
        {
            network.IMDS_TOKEN_URL: "a-token",
            network.IMDS_PUBLIC_IPV4_URL: ELASTIC_IP,
            "https://api.ipify.org": HOME_IP,
        },
    )

    assert network.outbound_ip() == ELASTIC_IP
    assert "https://api.ipify.org" not in fake.urls()


def test_the_metadata_read_is_imdsv2(monkeypatch):
    """The box requires IMDSv2: a GET without a session token is refused."""
    fake = install(
        monkeypatch,
        {network.IMDS_TOKEN_URL: "a-token", network.IMDS_PUBLIC_IPV4_URL: ELASTIC_IP},
    )

    network.outbound_ip()

    (put_method, put_url, put_headers, _), (get_method, get_url, get_headers, _) = fake.calls[:2]
    assert (put_method, put_url) == ("PUT", network.IMDS_TOKEN_URL)
    assert "x-aws-ec2-metadata-token-ttl-seconds" in put_headers
    assert (get_method, get_url) == ("GET", network.IMDS_PUBLIC_IPV4_URL)
    assert get_headers["x-aws-ec2-metadata-token"] == "a-token"


def test_off_ec2_it_falls_back_to_the_echo_services(monkeypatch):
    fake = install(monkeypatch, {"https://api.ipify.org": HOME_IP})

    assert network.outbound_ip() == HOME_IP
    assert fake.urls()[0] == network.IMDS_TOKEN_URL


def test_the_metadata_probe_is_short_enough_not_to_slow_a_laptop(monkeypatch):
    fake = install(monkeypatch, {"https://api.ipify.org": HOME_IP})

    network.outbound_ip()

    [(_, _, _, timeout)] = [call for call in fake.calls if call[1] == network.IMDS_TOKEN_URL]
    assert timeout <= 0.5


def test_an_instance_without_a_public_address_falls_back(monkeypatch):
    """public-ipv4 is a 404 on an instance with none; the echo still answers."""
    fake = install(
        monkeypatch,
        {network.IMDS_TOKEN_URL: "a-token", "https://checkip.amazonaws.com": HOME_IP},
    )

    assert network.outbound_ip() == HOME_IP
    assert network.IMDS_PUBLIC_IPV4_URL in fake.urls()


def test_junk_from_the_metadata_service_is_not_an_address(monkeypatch):
    install(
        monkeypatch,
        {
            network.IMDS_TOKEN_URL: "a-token",
            network.IMDS_PUBLIC_IPV4_URL: "<html>captive portal</html>",
            "https://api.ipify.org": HOME_IP,
        },
    )

    assert network.outbound_ip() == HOME_IP


def test_nothing_answering_is_none_never_a_guess(monkeypatch):
    install(monkeypatch, {})
    assert network.outbound_ip() is None


def test_the_answer_is_cached(monkeypatch):
    fake = install(
        monkeypatch,
        {network.IMDS_TOKEN_URL: "a-token", network.IMDS_PUBLIC_IPV4_URL: ELASTIC_IP},
    )

    network.outbound_ip()
    calls = len(fake.calls)
    network.outbound_ip()

    assert len(fake.calls) == calls
