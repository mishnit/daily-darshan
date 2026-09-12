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
    parser = Caption()
    parser.feed(page)
    assert "https://vipseva.com/?ref=9199" in parser.text
    assert "private-token" not in parser.text
    assert "/images/" not in parser.text


@pytest.mark.parametrize("scenario", ["success", "unsupported", "cancel", "fetch_error", "copy_error"])
def test_share_script_behaviour(scenario):
    node = os.environ.get("NODE_BINARY") or shutil.which("node")
    if not node:
        pytest.skip("Node required for native-share adapter tests")
    harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = {};
for (const id of ['share-darshan', 'share-caption', 'share-status', 'share-fallback', 'download-darshan', 'copy-caption']) {
  elements[id] = {hidden: true, open: false, value: 'caption with https://vipseva.com/?ref=9199', href: 'https://vipseva.com/images/today.jpg',
    addEventListener(event, cb) {this[event] = cb;}, focus() {}, select() {this.selected = true;}};
}
let shared;
const context = {document: {getElementById: id => elements[id]},
  File: class {constructor(parts, name, options) {this.name = name; this.type = options.type;}},
  URL: {createObjectURL: () => 'blob:image'},
  fetch: async () => ({ok: scenario !== 'fetch_error', blob: async () => ({type: 'image/jpeg'})}),
  navigator: {canShare: () => scenario !== 'unsupported', share: async data => {
    if (scenario === 'cancel') throw Object.assign(new Error(), {name: 'AbortError'});
    shared = data;
  }, clipboard: {writeText: async () => {if (scenario === 'copy_error') throw new Error();}}}};
vm.runInNewContext(source, context);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  await elements['share-darshan'].click();
  if (scenario === 'success') {
    assert.equal(shared.files[0].name, 'daily-darshan.jpg');
    assert.equal(shared.text, elements['share-caption'].value);
    assert.equal(shared.url, undefined);
  }
  if (['unsupported', 'fetch_error'].includes(scenario)) assert.equal(elements['share-fallback'].open, true);
  if (scenario === 'cancel') assert.equal(elements['share-fallback'].open, false);
  if (scenario === 'copy_error') {
    await elements['copy-caption'].click();
    assert.equal(elements['share-caption'].selected, true);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    subprocess.run([node, "-e", "const scenario=" + json.dumps(scenario) + ";const source=" + json.dumps(_SHARE_SCRIPT) + ";" + harness], check=True)
