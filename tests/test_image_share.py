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
    assert "https://wa.me/916361699109?text=" in page
    assert "ref%3D9199" in page
    assert 'href="https://vipseva.com/' not in page.split('id="share-darshan"', 1)[1].split('>', 1)[0]
    for control in ("download-darshan", "share-caption", "copy-caption", "share-fallback"):
        assert control not in page


@pytest.mark.parametrize("scenario", ["success"])
def test_share_script_behaviour(scenario):
    assert "fetch(" not in _SHARE_SCRIPT
    assert "navigator.share" not in _SHARE_SCRIPT
