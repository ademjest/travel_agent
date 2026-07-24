import unittest

from secure_download import download_https, validate_public_https_url


class FakeResponse:
    def __init__(self, body=b"ok", status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/octet-stream"}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("request failed for secret signed URL")

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


PUBLIC = lambda hostname: ("93.184.216.34",)


class SecureDownloadTests(unittest.TestCase):
    def test_non_https_url_is_rejected_before_request(self):
        session = FakeSession([])

        with self.assertRaisesRegex(ValueError, "HTTPS"):
            download_https(
                session,
                "http://example.com/file",
                max_bytes=10,
                timeout=1,
                resolver=PUBLIC,
            )

        self.assertEqual(session.urls, [])

    def test_private_dns_result_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "受限网络"):
            validate_public_https_url(
                "https://files.example.com/file",
                resolver=lambda hostname: ("127.0.0.1",),
            )

    def test_redirect_target_is_validated_before_following(self):
        session = FakeSession([
            FakeResponse(
                status_code=302,
                headers={"Location": "https://127.0.0.1/internal"},
            ),
        ])

        with self.assertRaisesRegex(ValueError, "受限网络"):
            download_https(
                session,
                "https://example.com/file",
                max_bytes=10,
                timeout=1,
                resolver=PUBLIC,
            )

        self.assertEqual(session.urls, ["https://example.com/file"])

    def test_streaming_limit_stops_oversized_body(self):
        session = FakeSession([FakeResponse(body=b"123456")])

        with self.assertRaisesRegex(ValueError, "超过"):
            download_https(
                session,
                "https://example.com/file",
                max_bytes=5,
                timeout=1,
                resolver=PUBLIC,
                size_error="文件超过 5 字节限制",
            )

    def test_http_error_does_not_expose_url(self):
        session = FakeSession([FakeResponse(status_code=500)])

        with self.assertRaises(ValueError) as raised:
            download_https(
                session,
                "https://example.com/file?token=secret",
                max_bytes=10,
                timeout=1,
                resolver=PUBLIC,
            )

        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("https://", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
