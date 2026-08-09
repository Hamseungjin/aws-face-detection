'use strict';

var fs = require('fs');
var vm = require('vm');
var assert = require('assert');

global.window = {
  __KIOSK_TEST_MODE__: true,
  setTimeout: function (callback) { callback(); return 1; },
  clearTimeout: function () {},
  addEventListener: function () {},
  speechSynthesis: { cancel: function () {} }
};
global.document = {
  addEventListener: function () {},
  removeEventListener: function () {}
};
Object.defineProperty(global, 'navigator', { value: {}, configurable: true });
global.axios = { create: function () { return {}; } };
global.Vue = function (options) { return options; };

var source = fs.readFileSync(process.argv[2], 'utf8');
vm.runInThisContext(source, { filename: process.argv[2] });

var hooks = window.__KIOSK_TEST_HOOKS__;
assert.ok(hooks);
assert.strictEqual(hooks.isCompletedVerificationOutcome('TARGET_FRAME_NOT_READY'), false);
assert.strictEqual(hooks.isCompletedVerificationOutcome('SIMILARITY_BELOW_THRESHOLD'), true);

var captureId = '7f5fd075-e711-4fdd-8d2a-ffcd5c6f65b1';
var targetIds = [];
var outcomes = [
  { success: false, matched: false, reason: 'TARGET_FRAME_NOT_READY' },
  { success: false, matched: false, reason: 'TARGET_FRAME_NOT_READY' },
  { success: true, matched: true, similarity: 96, reason: 'SIMILARITY_ABOVE_THRESHOLD' }
];
var captureClient = {
  post: function (path) {
    assert.strictEqual(path, 'capture-frame');
    return Promise.resolve({ data: { ok: true, captureId: captureId } });
  }
};
var gateway = {
  post: function (path, payload) {
    assert.strictEqual(path, 'face-compare');
    assert.strictEqual(payload.similarityThreshold, 90);
    targetIds.push(payload.targetFrameId);
    return Promise.resolve({ data: outcomes.shift() });
  }
};
var service = new hooks.PipelineService(captureClient);
service.gatewayClient = gateway;

service.sendCapturedFrame('camera-image', 1).then(function (capture) {
  return service.waitForExactComparison(
    { base64: 'id-image', filename: 'id.jpg', contentType: 'image/jpeg' },
    capture.captureId,
    function () { return false; }
  );
}).then(function (result) {
  assert.strictEqual(result.reason, 'SIMILARITY_ABOVE_THRESHOLD');
  assert.deepStrictEqual(targetIds, [captureId, captureId, captureId]);
}).catch(function (error) {
  process.nextTick(function () { throw error; });
  process.exitCode = 1;
});
