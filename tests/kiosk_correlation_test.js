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
assert.strictEqual(hooks.isCompletedVerificationOutcome('NO_FACE'), false);
assert.strictEqual(hooks.isCompletedVerificationOutcome('TOO_BLURRY'), false);
assert.strictEqual(hooks.isQualityGateFailure('NO_FACE'), true);
assert.strictEqual(hooks.isQualityGateFailure('MULTIPLE_FACES'), true);
assert.strictEqual(hooks.isQualityGateFailure('FACE_TOO_SMALL'), true);
assert.strictEqual(hooks.isQualityGateFailure('FACE_OFF_CENTER'), true);
assert.strictEqual(hooks.isQualityGateFailure('TOO_DARK'), true);
assert.strictEqual(hooks.isQualityGateFailure('TOO_BRIGHT'), true);
assert.strictEqual(hooks.isQualityGateFailure('TOO_BLURRY'), true);
assert.strictEqual(hooks.isQualityGateFailure('FACE_TILTED'), true);
assert.strictEqual(hooks.isQualityGateFailure('FACE_POSE_INVALID'), true);
assert.strictEqual(hooks.isCompletedVerificationOutcome('FACE_TILTED'), false);
assert.strictEqual(hooks.isCompletedVerificationOutcome('FACE_POSE_INVALID'), false);
assert.strictEqual(hooks.isQualityGateFailure('SIMILARITY_BELOW_THRESHOLD'), false);
assert.strictEqual(hooks.isQualityGateFailure('INVALID_IMAGE'), false);
assert.strictEqual(hooks.isQualityGateFailure('INVALID_IMAGE', 'quality_gate'), true);
assert.strictEqual(hooks.isQualityGateFailure('INVALID_IMAGE', 'id_image'), false);
assert.strictEqual(hooks.isQualityGateFailure('UNSUPPORTED_IMAGE_FORMAT', 'frame_a'), true);
assert.strictEqual(hooks.isIdImageSource('id_image'), true);
assert.strictEqual(hooks.isIdImageSource('input'), true);
assert.strictEqual(hooks.isCameraSource('quality_gate'), true);

var idInvalid = hooks.friendlyVerificationMessage('INVALID_IMAGE', 'id_image');
assert.ok(idInvalid.indexOf('신분증') !== -1, idInvalid);
var qualityInvalid = hooks.friendlyVerificationMessage('INVALID_IMAGE', 'quality_gate');
assert.ok(qualityInvalid.indexOf('카메라') !== -1, qualityInvalid);
assert.notStrictEqual(idInvalid, qualityInvalid);
var noFace = hooks.friendlyVerificationMessage('NO_FACE', 'quality_gate');
assert.ok(noFace.indexOf('얼굴') !== -1, noFace);
var detectorDown = hooks.friendlyVerificationMessage('QUALITY_DETECTOR_UNAVAILABLE', 'quality_gate');
assert.ok(detectorDown.indexOf('준비하지') !== -1, detectorDown);
var tilted = hooks.friendlyVerificationMessage('FACE_TILTED', 'quality_gate');
assert.ok(tilted.indexOf('고개') !== -1, tilted);
var poseInvalid = hooks.friendlyVerificationMessage('FACE_POSE_INVALID', 'quality_gate');
assert.ok(poseInvalid.indexOf('정면') !== -1, poseInvalid);
var qualityBlur = hooks.friendlyVerificationMessage('TOO_BLURRY', 'quality_gate');
assert.ok(qualityBlur.indexOf('신분증') === -1, qualityBlur);
assert.ok(hooks.TRANSITIONS.FACE_RETRY.indexOf('ID_CAPTURE') !== -1);
assert.strictEqual(hooks.isKnownStoreCompleteFailure('FACE_COLLECTION_NOT_CONFIGURED'), true);
assert.strictEqual(hooks.isKnownStoreCompleteFailure('FACE_INDEX_FAILED'), true);
var collectionDown = hooks.friendlyVerificationMessage('FACE_COLLECTION_NOT_CONFIGURED');
assert.ok(collectionDown.indexOf('설정') !== -1, collectionDown);
var indexFail = hooks.friendlyVerificationMessage('FACE_INDEX_FAILED');
assert.ok(indexFail.indexOf('등록') !== -1, indexFail);
assert.ok(hooks.TRANSITIONS.FACE_RETRY.indexOf('FACE_CAPTURE') !== -1);
assert.strictEqual(hooks.shouldApplyIdRead(3, 3), true);
assert.strictEqual(hooks.shouldApplyIdRead(2, 3), false);
var fakeInput = { value: 'id-a.jpg' };
hooks.resetFileInput(fakeInput);
assert.strictEqual(fakeInput.value, '');
var idA = { filename: 'a.jpg', contentType: 'image/jpeg', base64: 'AAA' };
var idB = { filename: 'b.png', contentType: 'image/png', base64: 'BBB' };
var currentGeneration = 1;
var applied = null;
function applyIfCurrent(generation, image) {
  if (hooks.shouldApplyIdRead(generation, currentGeneration)) {
    applied = image;
  }
}
applyIfCurrent(1, idA);
currentGeneration = 2;
applyIfCurrent(1, idA);
applyIfCurrent(2, idB);
assert.strictEqual(applied.filename, 'b.png');
assert.strictEqual(applied.base64, 'BBB');
assert.notStrictEqual(applied.base64, idA.base64);

assert.strictEqual(hooks.KIOSK_JS_BUILD, 'id-file-read-1');
assert.strictEqual(hooks.filenameExtension('photo.JPG'), 'jpg');
assert.strictEqual(hooks.filenameExtension('a.png'), 'png');

function fakeFile(name, type, size) {
  return { name: name, type: type, size: size, lastModified: 1700000000000 };
}

var jpegOk = hooks.validateIdFile(fakeFile('id.jpg', 'image/jpeg', 1200));
assert.strictEqual(jpegOk.ok, true);
assert.strictEqual(jpegOk.contentType, 'image/jpeg');
var pngOk = hooks.validateIdFile(fakeFile('id.png', 'image/png', 1200));
assert.strictEqual(pngOk.ok, true);
var emptyTypeJpg = hooks.validateIdFile(fakeFile('id.jpg', '', 1200));
assert.strictEqual(emptyTypeJpg.ok, true);
assert.strictEqual(emptyTypeJpg.contentType, 'image/jpeg');
var emptyTypePng = hooks.validateIdFile(fakeFile('id.png', '', 800));
assert.strictEqual(emptyTypePng.ok, true);
assert.strictEqual(emptyTypePng.contentType, 'image/png');
var mismatchStillOk = hooks.inferImageContentType('shot.jpg', 'image/png');
assert.strictEqual(mismatchStillOk.contentType, 'image/png');
var emptyTypeUsesExt = hooks.inferImageContentType('shot.jpg', '');
assert.strictEqual(emptyTypeUsesExt.contentType, 'image/jpeg');
var zero = hooks.validateIdFile(fakeFile('id.jpg', 'image/jpeg', 0));
assert.strictEqual(zero.ok, false);
assert.strictEqual(zero.code, 'ZERO_BYTE_FILE');
var huge = hooks.validateIdFile(fakeFile('id.jpg', 'image/jpeg', 6 * 1024 * 1024));
assert.strictEqual(huge.ok, false);
assert.strictEqual(huge.code, 'IMAGE_TOO_LARGE');
assert.ok(huge.message.indexOf('5MiB') !== -1);
var noFile = hooks.validateIdFile(null);
assert.strictEqual(noFile.code, 'NO_FILE');
var badExt = hooks.validateIdFile(fakeFile('id.heic', 'image/heic', 100));
assert.strictEqual(badExt.code, 'UNSUPPORTED_FILE');
assert.notStrictEqual(badExt.message, '사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.');

var validDataUrl = hooks.parseIdDataUrl('data:image/jpeg;base64,abc123');
assert.strictEqual(validDataUrl.ok, true);
assert.strictEqual(validDataUrl.mime, 'image/jpeg');
assert.strictEqual(validDataUrl.payloadLength, 6);
assert.strictEqual(hooks.parseIdDataUrl('not-a-data-url').ok, false);
assert.strictEqual(hooks.parseIdDataUrl('data:image/jpeg;base64,').ok, false);

function readerCtor(outcomeFn) {
  function FakeReader() {
    this.result = '';
    this.error = null;
  }
  FakeReader.prototype.readAsDataURL = function () {
    var outcome = outcomeFn();
    if (outcome.kind === 'error') {
      this.error = { name: outcome.name || 'NotReadableError' };
      this.onerror();
      return;
    }
    if (outcome.kind === 'abort') {
      this.onabort();
      return;
    }
    this.result = outcome.dataUrl;
    this.onload({ target: this });
  };
  return FakeReader;
}

var kept = fakeFile('keep.jpg', 'image/jpeg', 99);
var picker = { value: 'C:\\fakepath\\keep.jpg', files: [kept] };
var captured = hooks.captureSelectedFile(picker);
hooks.resetFileInput(picker);
assert.strictEqual(captured, kept);
assert.strictEqual(captured.size, 99);
assert.strictEqual(picker.value, '');

var currentGen = 7;
var shownError = null;
var assignedImage = null;
function applyRead(generation, result, isError) {
  if (!hooks.shouldApplyIdRead(generation, currentGen)) {
    return;
  }
  if (isError) { shownError = result; } else { assignedImage = result; }
}
applyRead(6, '사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.', true);
assert.strictEqual(shownError, null);
applyRead(7, { base64: 'NEW' }, false);
assert.strictEqual(assignedImage.base64, 'NEW');

function expectReject(promise, code) {
  return promise.then(function () {
    throw new Error('expected reject ' + code);
  }, function (error) {
    assert.strictEqual(error.code, code);
  });
}

var idFileTests = Promise.resolve()
  .then(function () {
    hooks.DemoIdAdapter.createReader = readerCtor(function () {
      return { kind: 'ok', dataUrl: 'data:image/jpeg;base64,QUJD' };
    });
    return hooks.DemoIdAdapter.read(fakeFile('a.jpg', 'image/jpeg', 20));
  })
  .then(function (image) {
    assert.strictEqual(image.contentType, 'image/jpeg');
    assert.strictEqual(image.base64, 'QUJD');
    assert.ok(image.previewUrl.indexOf('data:image/jpeg') === 0);
  })
  .then(function () {
    var failTries = 0;
    hooks.DemoIdAdapter.createReader = readerCtor(function () {
      failTries += 1;
      if (failTries < 2) { return { kind: 'error', name: 'NotReadableError' }; }
      return { kind: 'ok', dataUrl: 'data:image/png;base64,UE5H' };
    });
    return hooks.DemoIdAdapter.read(fakeFile('b.png', '', 20)).then(function (image) {
      assert.strictEqual(image.contentType, 'image/png');
      assert.strictEqual(image.base64, 'UE5H');
      assert.ok(failTries >= 2);
    });
  })
  .then(function () {
    hooks.DemoIdAdapter.createReader = readerCtor(function () {
      return { kind: 'error', name: 'NotReadableError' };
    });
    return expectReject(
      hooks.DemoIdAdapter.read(fakeFile('c.jpg', 'image/jpeg', 20)),
      'FILE_READ_FAILED'
    );
  })
  .then(function () {
    hooks.DemoIdAdapter.createReader = readerCtor(function () {
      return { kind: 'abort' };
    });
    return expectReject(
      hooks.DemoIdAdapter.read(fakeFile('d.jpg', 'image/jpeg', 20)),
      'FILE_READ_ABORTED'
    );
  })
  .then(function () {
    hooks.DemoIdAdapter.createReader = readerCtor(function () {
      return { kind: 'ok', dataUrl: 'not-valid' };
    });
    return expectReject(
      hooks.DemoIdAdapter.read(fakeFile('e.jpg', 'image/jpeg', 20)),
      'INVALID_DATA_URL'
    );
  });

idFileTests.catch(function (error) {
  process.nextTick(function () { throw error; });
  process.exitCode = 1;
});
assert.ok(hooks.KioskTransactionService);
assert.strictEqual(typeof hooks.KioskTransactionService.precheckRetrievalCode, 'function');
assert.ok(String(hooks.KioskTransactionService.precheckRetrievalCode).indexOf('retrieve/lookup') !== -1);
assert.strictEqual(hooks.isCompletedVerificationOutcome('INVALID_IMAGE'), false);
assert.ok(hooks.TRANSITIONS.MODE_SELECT.indexOf('LOCKER_SELECT') !== -1);
assert.ok(hooks.TRANSITIONS.MODE_SELECT.indexOf('CONSENT') === -1);
assert.ok(hooks.TRANSITIONS.LOCKER_RESERVING.indexOf('CONSENT') !== -1);
assert.ok(hooks.TRANSITIONS.VERIFYING.indexOf('PAYMENT') !== -1);

var faceBytes = 'frame-a-jpeg-base64';
var posted = [];
var captureClient = {
  post: function (path, payload) {
    posted.push({ path: path, payload: payload });
    assert.notStrictEqual(path, 'capture-frame');
    if (path === 'kiosk/store/face-verify') {
      assert.strictEqual(payload.faceImageBase64, faceBytes);
      assert.strictEqual(payload.transactionId, 'hold-tx-1');
      assert.strictEqual(payload.targetFrameId, undefined);
      return Promise.resolve({
        data: {
          success: true,
          matched: true,
          similarity: 96,
          reason: 'SIMILARITY_ABOVE_THRESHOLD'
        }
      });
    }
    if (path === 'kiosk/retrieve/lookup') {
      assert.strictEqual(payload.retrievalCode, '12345678');
      assert.strictEqual(payload.faceImageBase64, undefined);
      return Promise.resolve({ data: { ready: true, status: 'STORED' } });
    }
    if (path === 'kiosk/retrieve/start') {
      assert.strictEqual(payload.faceImageBase64, faceBytes);
      assert.strictEqual(payload.targetFrameId, undefined);
      return Promise.resolve({
        data: {
          success: true,
          matched: true,
          similarity: 97,
          reason: 'SIMILARITY_ABOVE_THRESHOLD',
          status: 'RETRIEVING'
        }
      });
    }
    throw new Error('unexpected path ' + path);
  }
};
var service = new hooks.PipelineService(captureClient);

service.waitForExactComparison(
  { base64: 'id-image', filename: 'id.jpg', contentType: 'image/jpeg' },
  faceBytes,
  function () { return false; },
  'hold-tx-1'
).then(function (result) {
  assert.strictEqual(result.reason, 'SIMILARITY_ABOVE_THRESHOLD');
  return service.waitForRetrievalComparison('12345678', faceBytes, function () { return false; });
}).then(function (result) {
  assert.strictEqual(result.status, 'RETRIEVING');
  assert.strictEqual(posted.length, 2);
  assert.strictEqual(posted[0].path, 'kiosk/store/face-verify');
  assert.strictEqual(posted[1].path, 'kiosk/retrieve/start');
  assert.ok(posted.every(function (entry) {
    return entry.path !== 'capture-frame' && !entry.payload.targetFrameId;
  }));
}).catch(function (error) {
  process.nextTick(function () { throw error; });
  process.exitCode = 1;
});
