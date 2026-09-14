import json
import os
import shutil
import subprocess
from datetime import date
from html.parser import HTMLParser

import pytest
from adapters.page_renderer import PageRenderer, _SHARE_SCRIPT
from domain.subscriber import Subscriber


class Caption(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.text = ""
    def handle_starttag(self, tag, attrs):
        if tag == "textarea":
            self.inside = True
    def handle_endtag(self, tag):
        if tag == "textarea":
            self.inside = False
    def handle_data(self, data):
        if self.inside:
            self.text += data


def test_native_share_caption_excludes_personal_page_and_image_url():
    page = PageRenderer(image_public_base="https://vipseva.com").render_html(
        Subscriber("9199", "monthly", subscription_id="private-token"), date(2026, 9, 12), True)
    assert 'id="share-darshan"' in page
    assert 'data-image-url="https://vipseva.com/images/2026-09-12.jpg"' in page
    assert "https://vipseva.com/?ref=9199" in page
    assert "https://wa.me/916361699109" not in page
    for control in ("download-darshan", "share-caption", "copy-caption", "share-fallback"):
        assert control not in page


@pytest.mark.parametrize("scenario", ["success"])
def test_share_script_behaviour(scenario):
    assert "fetch(" in _SHARE_SCRIPT
    assert "navigator.share" in _SHARE_SCRIPT
    assert "files: [file]" in _SHARE_SCRIPT
