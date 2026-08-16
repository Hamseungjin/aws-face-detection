(function () {
  'use strict';

  var KIOSK_STATES = Object.freeze({
    ATTRACT: 'ATTRACT',
    MODE_SELECT: 'MODE_SELECT',
    CONSENT: 'CONSENT',
    ID_CAPTURE: 'ID_CAPTURE',
    FACE_CAPTURE: 'FACE_CAPTURE',
    WAITING_FOR_FRAME: 'WAITING_FOR_FRAME',
    VERIFYING: 'VERIFYING',
    FACE_RETRY: 'FACE_RETRY',
    LOCKER_SELECT: 'LOCKER_SELECT',
    LOCKER_RESERVING: 'LOCKER_RESERVING',
    PAYMENT: 'PAYMENT',
    STORE_COMPLETING: 'STORE_COMPLETING',
    RETRIEVAL_CODE: 'RETRIEVAL_CODE',
    RETRIEVE_CONFIRM: 'RETRIEVE_CONFIRM',
    RETRIEVE_SETTLEMENT: 'RETRIEVE_SETTLEMENT',
    LOCKER_OPENING: 'LOCKER_OPENING',
    COMPLETE: 'COMPLETE',
    HELP: 'HELP'
  });

  var TRANSITIONS = Object.freeze({
    ATTRACT: ['MODE_SELECT'],
    // STORE: consent + ID. RETRIEVE: code first (no ID re-verification).
    MODE_SELECT: ['LOCKER_SELECT', 'RETRIEVAL_CODE', 'ATTRACT', 'HELP'],
    CONSENT: ['ID_CAPTURE', 'LOCKER_SELECT', 'ATTRACT', 'HELP'],
    ID_CAPTURE: ['FACE_CAPTURE', 'ATTRACT', 'HELP'],
    FACE_CAPTURE: ['WAITING_FOR_FRAME', 'ATTRACT', 'HELP'],
    WAITING_FOR_FRAME: ['VERIFYING', 'FACE_CAPTURE', 'FACE_RETRY', 'LOCKER_SELECT', 'ATTRACT', 'HELP'],
    VERIFYING: ['PAYMENT', 'LOCKER_OPENING', 'FACE_RETRY', 'LOCKER_SELECT', 'ATTRACT', 'HELP'],
    FACE_RETRY: ['FACE_CAPTURE', 'ID_CAPTURE', 'LOCKER_SELECT', 'ATTRACT', 'HELP'],
    LOCKER_SELECT: ['LOCKER_RESERVING', 'MODE_SELECT', 'ATTRACT', 'HELP'],
    LOCKER_RESERVING: ['LOCKER_SELECT', 'CONSENT', 'ATTRACT', 'HELP'],
    PAYMENT: ['LOCKER_SELECT', 'STORE_COMPLETING', 'ATTRACT', 'HELP'],
    STORE_COMPLETING: ['LOCKER_OPENING', 'ATTRACT', 'HELP'],
    RETRIEVAL_CODE: ['FACE_CAPTURE', 'ATTRACT', 'HELP'],
    // Legacy screens retained for recovery paths; primary retrieve flow skips them.
    RETRIEVE_CONFIRM: ['RETRIEVAL_CODE', 'RETRIEVE_SETTLEMENT', 'LOCKER_OPENING', 'ATTRACT', 'HELP'],
    RETRIEVE_SETTLEMENT: ['LOCKER_OPENING', 'ATTRACT', 'HELP'],
    LOCKER_OPENING: ['COMPLETE', 'ATTRACT', 'HELP'],
    COMPLETE: ['ATTRACT'],
    HELP: ['ATTRACT']
  });

  var SIMILARITY_THRESHOLD = 90;
  var MAX_ID_IMAGE_BYTES = 5 * 1024 * 1024;
  var ID_FILE_READ_ATTEMPTS = 3;
  var ID_FILE_READ_RETRY_MS = 40;
  var KIOSK_JS_BUILD = 'id-file-read-1';
  var IDLE_TIMEOUT_MS = 60000;
  var MAX_VERIFICATION_ATTEMPTS = 3;

  // Under code-server subpath proxy (e.g. /proxy/8080/), prefix API calls so the
  // browser hits /proxy/<port>/api/... instead of /api/... on the outer host.
  function getApiBaseUrl(pathname) {
    var path = pathname || (typeof window !== 'undefined' && window.location && window.location.pathname) || '';
    var match = String(path).match(/^(\/proxy\/\d+)/);
    return (match ? match[1] : '') + '/api';
  }

  var apiClient = axios.create({
    baseURL: getApiBaseUrl(),
    withCredentials: true,
    timeout: 20000
  });

  function delay(milliseconds) {
    return new Promise(function (resolve) { window.setTimeout(resolve, milliseconds); });
  }

  function cancelledError() {
    var error = new Error('operation cancelled');
    error.code = 'CANCELLED';
    return error;
  }

  function isAmbiguousRequestError(error) {
    return !error || !error.response || Number(error.response.status) >= 500;
  }

  function httpErrorDetail(error) {
    var detail = error && error.response && error.response.data && error.response.data.detail;
    return typeof detail === 'string' ? detail : '';
  }

  function isKnownStoreCompleteFailure(reason) {
    return ['FACE_COLLECTION_NOT_CONFIGURED', 'FACE_INDEX_FAILED', 'FACE_ID_MISSING',
      'ACCESS_DENIED', 'THROTTLED', 'AWS_API_ERROR', 'INVALID_IMAGE',
      'IMAGE_TOO_LARGE', 'NO_FACE_IN_SOURCE_OR_TARGET'].indexOf(reason) !== -1;
  }

  function isCompletedVerificationOutcome(reason) {
    return ['SIMILARITY_ABOVE_THRESHOLD', 'SIMILARITY_BELOW_THRESHOLD',
      'NO_FACE_IN_SOURCE_OR_TARGET', 'NO_FACE_IN_REFERENCE', 'NO_FACE_IN_TARGET',
      'INVALID_RETRIEVAL_CODE', 'REFERENCE_FACE_MISSING',
      'EXPECTED_FACE_NOT_FOUND'].indexOf(reason) !== -1;
  }

  function isImageFormatReason(reason) {
    return ['INVALID_IMAGE', 'UNSUPPORTED_IMAGE_FORMAT', 'IMAGE_TOO_LARGE'].indexOf(reason) !== -1;
  }

  function isCameraSource(source) {
    return source === 'quality_gate' || source === 'frame_a' || source === 'frame_b';
  }

  function isIdImageSource(source) {
    return source === 'id_image' || source === 'input';
  }

  function resetFileInput(input) {
    if (input) { input.value = ''; }
  }

  function shouldApplyIdRead(readGeneration, currentGeneration) {
    return Number(readGeneration) === Number(currentGeneration);
  }

  function captureSelectedFile(input) {
    return (input && input.files && input.files[0]) || null;
  }

  function filenameExtension(filename) {
    if (!filename || typeof filename !== 'string') { return ''; }
    var parts = filename.split('.');
    if (parts.length < 2) { return ''; }
    return String(parts.pop() || '').toLowerCase();
  }

  function inferImageContentType(filename, fileType) {
    var extension = filenameExtension(filename);
    var inferredType = extension === 'png' ? 'image/png' :
      (extension === 'jpg' || extension === 'jpeg' ? 'image/jpeg' : '');
    var declared = String(fileType || '').toLowerCase();
    if (declared === 'image/jpg') { declared = 'image/jpeg'; }
    var contentType = (declared === 'image/jpeg' || declared === 'image/png') ?
      declared : inferredType;
    return {
      extension: extension,
      inferredType: inferredType,
      declaredType: declared,
      contentType: contentType
    };
  }

  function createIdFileError(code, message, extra) {
    var error = new Error(message);
    error.code = code;
    if (extra) {
      Object.keys(extra).forEach(function (key) { error[key] = extra[key]; });
    }
    return error;
  }

  function logIdFile(stage, details) {
    if (typeof console === 'undefined' || !console.info) { return; }
    var payload = { event: 'id_file_read', stage: stage };
    if (details) {
      Object.keys(details).forEach(function (key) { payload[key] = details[key]; });
    }
    console.info('[kiosk:id-file]', payload);
  }

  function parseIdDataUrl(dataUrl) {
    if (typeof dataUrl !== 'string' || !dataUrl) {
      return { ok: false, code: 'INVALID_DATA_URL' };
    }
    var separator = dataUrl.indexOf(',');
    if (separator < 0) {
      return { ok: false, code: 'INVALID_DATA_URL' };
    }
    var header = dataUrl.slice(0, separator);
    var payload = dataUrl.slice(separator + 1);
    if (!payload) {
      return { ok: false, code: 'INVALID_DATA_URL' };
    }
    var mimeMatch = header.match(/^data:([^;,]+)/i);
    return {
      ok: true,
      mime: mimeMatch ? mimeMatch[1].toLowerCase() : '',
      payloadLength: payload.length,
      previewUrl: dataUrl,
      base64: payload
    };
  }

  function validateIdFile(file) {
    if (!file) {
      return {
        ok: false,
        code: 'NO_FILE',
        message: '사진 파일을 선택해 주세요.'
      };
    }
    var meta = inferImageContentType(file.name, file.type);
    var size = Number(file.size || 0);
    if (!size) {
      return {
        ok: false,
        code: 'ZERO_BYTE_FILE',
        message: '사진 파일을 읽지 못했습니다. 다른 사진을 선택해 주세요.',
        extension: meta.extension,
        fileType: file.type || '',
        fileSize: size
      };
    }
    if (size > MAX_ID_IMAGE_BYTES) {
      return {
        ok: false,
        code: 'IMAGE_TOO_LARGE',
        message: '사진 크기는 5MiB 이하여야 합니다.',
        extension: meta.extension,
        fileType: file.type || '',
        fileSize: size
      };
    }
    if (!meta.contentType) {
      return {
        ok: false,
        code: 'UNSUPPORTED_FILE',
        message: 'JPG, JPEG 또는 PNG 사진만 선택할 수 있습니다.',
        extension: meta.extension,
        fileType: file.type || '',
        fileSize: size
      };
    }
    return {
      ok: true,
      code: 'OK',
      extension: meta.extension,
      contentType: meta.contentType,
      fileType: file.type || '',
      fileSize: size,
      lastModifiedPresent: !!file.lastModified
    };
  }

  function readBlobAsDataUrl(blob) {
    return new Promise(function (resolve, reject) {
      var Reader = DemoIdAdapter.createReader ||
        (typeof FileReader !== 'undefined' ? FileReader : null);
      if (!Reader) {
        reject(createIdFileError(
          'FILE_READ_FAILED',
          '사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.',
          { errorName: 'FileReaderUnavailable' }
        ));
        return;
      }
      var reader = new Reader();
      reader.onload = function (event) {
        var result = (event && event.target && event.target.result) || reader.result;
        resolve(result);
      };
      reader.onerror = function () {
        var name = (reader.error && reader.error.name) || 'NotReadableError';
        reject(createIdFileError(
          'FILE_READ_FAILED',
          '사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.',
          { errorName: name }
        ));
      };
      reader.onabort = function () {
        reject(createIdFileError(
          'FILE_READ_ABORTED',
          '사진 읽기가 중단되었습니다. 다시 선택해 주세요.',
          { errorName: 'AbortError' }
        ));
      };
      try {
        reader.readAsDataURL(blob);
      } catch (error) {
        reject(createIdFileError(
          'FILE_READ_FAILED',
          '사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.',
          { errorName: (error && error.name) || 'Error' }
        ));
      }
    });
  }

  function readBlobAsDataUrlWithRetry(blob, attemptsLeft) {
    var remaining = attemptsLeft == null ? ID_FILE_READ_ATTEMPTS : attemptsLeft;
    return readBlobAsDataUrl(blob).catch(function (error) {
      if (error && error.code === 'FILE_READ_ABORTED') { throw error; }
      if (remaining <= 1) { throw error; }
      logIdFile('retry', {
        result: 'FAIL',
        error_name: error && error.errorName,
        attempts_left: remaining - 1
      });
      return delay(ID_FILE_READ_RETRY_MS).then(function () {
        return readBlobAsDataUrlWithRetry(blob, remaining - 1);
      });
    });
  }

  function isQualityGateFailure(reason, source) {
    if (isCameraSource(source)) { return true; }
    return ['NO_FACE', 'MULTIPLE_FACES', 'FACE_TOO_SMALL', 'FACE_OFF_CENTER',
      'TOO_DARK', 'TOO_BRIGHT', 'TOO_BLURRY', 'FACE_TILTED', 'FACE_POSE_INVALID',
      'QUALITY_CHECK_FAILED', 'QUALITY_DETECTOR_UNAVAILABLE'].indexOf(reason) !== -1;
  }

  // Demo boundary: replace with an approved mobile-ID or physical-ID integration.
  var DemoIdAdapter = {
    createReader: null,
    read: function (file) {
      var checked = validateIdFile(file);
      logIdFile('selected', {
        result: checked.ok ? 'PASS' : 'FAIL',
        extension: checked.extension || filenameExtension(file && file.name),
        file_type: (file && file.type) || '(empty)',
        file_size: file && typeof file.size === 'number' ? file.size : null,
        last_modified_present: !!(file && file.lastModified),
        code: checked.code
      });
      if (!checked.ok) {
        return Promise.reject(createIdFileError(checked.code, checked.message, {
          extension: checked.extension,
          fileSize: checked.fileSize,
          fileType: checked.fileType
        }));
      }
      logIdFile('filereader', {
        result: 'START',
        extension: checked.extension,
        file_type: checked.fileType || '(empty)',
        file_size: checked.fileSize
      });
      return readBlobAsDataUrlWithRetry(file).then(function (dataUrl) {
        var parsed = parseIdDataUrl(dataUrl);
        if (!parsed.ok) {
          logIdFile('read-failed', {
            result: 'FAIL',
            stage: 'dataurl',
            error_name: parsed.code,
            extension: checked.extension,
            file_size: checked.fileSize
          });
          throw createIdFileError(
            'INVALID_DATA_URL',
            '사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.',
            { errorName: parsed.code }
          );
        }
        logIdFile('read-success', {
          result: 'PASS',
          extension: checked.extension,
          data_url_prefix: parsed.mime || '(none)',
          base64_length: parsed.payloadLength
        });
        return {
          filename: file.name || ('id.' + (checked.extension || 'jpg')),
          contentType: checked.contentType,
          base64: parsed.base64,
          previewUrl: parsed.previewUrl,
          extension: checked.extension
        };
      }).catch(function (error) {
        if (!error || !error.code) {
          throw createIdFileError(
            'FILE_READ_FAILED',
            '사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.',
            { errorName: 'Error' }
          );
        }
        if (error.code !== 'FILE_READ_STALE') {
          logIdFile('read-failed', {
            result: 'FAIL',
            error_name: error.errorName || error.code,
            code: error.code,
            extension: checked.extension,
            file_size: checked.fileSize
          });
        }
        throw error;
      });
    }
  };

  function PipelineService(client) {
    this.client = client;
    this.gatewayClient = null;
  }

  PipelineService.prototype.clearGateway = function () { this.gatewayClient = null; };
  PipelineService.prototype.getGateway = function () {
    var self = this;
    if (this.gatewayClient) { return Promise.resolve(this.gatewayClient); }
    return this.client.get('config').then(function (response) {
      self.gatewayClient = axios.create({
        baseURL: response.data.apiBaseUrl,
        headers: { 'X-api-key': response.data.apiKey },
        timeout: 20000
      });
      return self.gatewayClient;
    });
  };
  // STORE: ID bytes vs FRAME-A bytes via FastAPI → Rekognition. No Kinesis.
  PipelineService.prototype.compareStoreFace = function (idImage, faceImageBase64, transactionId) {
    return this.client.post('kiosk/store/face-verify', {
      imageBase64: idImage.base64,
      filename: idImage.filename,
      contentType: idImage.contentType,
      faceImageBase64: faceImageBase64,
      transactionId: transactionId
    }).then(function (response) {
      return response.data;
    }).catch(function (error) {
      if (error.response && error.response.data && error.response.data.reason) {
        return error.response.data;
      }
      if (error.response && error.response.data && error.response.data.detail) {
        return {
          success: false,
          matched: false,
          reason: error.response.data.detail
        };
      }
      throw error;
    });
  };
  PipelineService.prototype.compareFace = function (idImage, faceImageBase64, transactionId) {
    return this.compareStoreFace(idImage, faceImageBase64, transactionId);
  };
  PipelineService.prototype.waitForExactComparison = function (
    idImage, faceImageBase64, isCancelled, transactionId
  ) {
    if (isCancelled()) { return Promise.reject(cancelledError()); }
    return this.compareStoreFace(idImage, faceImageBase64, transactionId);
  };
  // RETRIEVE: server resolves expected FaceId from retrieval_code; client sends
  // FRAME-B bytes only. Browser never sees FaceId or locker internals.
  PipelineService.prototype.waitForRetrievalComparison = function (
    retrievalCode, faceImageBase64, isCancelled
  ) {
    if (isCancelled()) { return Promise.reject(cancelledError()); }
    return this.client.post('kiosk/retrieve/start', {
      retrievalCode: retrievalCode,
      faceImageBase64: faceImageBase64
    }).then(function (response) {
      return response.data;
    }).catch(function (error) {
      if (error.response && error.response.status === 429) {
        var limited = new Error('rate limited');
        limited.code = 'RETRIEVAL_RATE_LIMITED';
        throw limited;
      }
      if (error.response && error.response.data && error.response.data.reason) {
        return error.response.data;
      }
      if (error.response && error.response.status === 409) {
        return {
          success: false,
          matched: false,
          reason: 'INVALID_RETRIEVAL_CODE'
        };
      }
      throw error;
    });
  };

  var pipelineService = new PipelineService(apiClient);

  // Opt-in unit-test seam; absent during normal kiosk operation.
  if (window.__KIOSK_TEST_MODE__) {
    window.__KIOSK_TEST_HOOKS__ = {
      PipelineService: PipelineService,
      isCompletedVerificationOutcome: isCompletedVerificationOutcome,
      isQualityGateFailure: isQualityGateFailure,
      isImageFormatReason: isImageFormatReason,
      isCameraSource: isCameraSource,
      isIdImageSource: isIdImageSource,
      httpErrorDetail: httpErrorDetail,
      isKnownStoreCompleteFailure: isKnownStoreCompleteFailure,
      friendlyVerificationMessage: friendlyVerificationMessage,
      getApiBaseUrl: getApiBaseUrl,
      TRANSITIONS: TRANSITIONS,
      KIOSK_STATES: KIOSK_STATES,
      DemoIdAdapter: DemoIdAdapter,
      resetFileInput: resetFileInput,
      shouldApplyIdRead: shouldApplyIdRead,
      captureSelectedFile: captureSelectedFile,
      inferImageContentType: inferImageContentType,
      validateIdFile: validateIdFile,
      parseIdDataUrl: parseIdDataUrl,
      filenameExtension: filenameExtension,
      KIOSK_JS_BUILD: KIOSK_JS_BUILD
    };
  }

  // Same-host FastAPI/SQLite boundary. No ID or face image is sent to these APIs.
  var KioskTransactionService = {
    listLockers: function () {
      return apiClient.get('kiosk/lockers').then(function (response) { return response.data.lockers; });
    },
    reserveLocker: function (lockerId) {
      return apiClient.post('kiosk/store/reserve', { lockerId: lockerId }).then(function (response) { return response.data; });
    },
    cancelStore: function (transactionId) {
      return apiClient.post('kiosk/store/cancel', { transactionId: transactionId }).then(function (response) { return response.data; });
    },
    completeStore: function (transactionId, verifiedFaceImageBase64) {
      return apiClient.post('kiosk/store/complete', {
        transactionId: transactionId,
        verifiedFaceImageBase64: verifiedFaceImageBase64
      }).then(function (response) { return response.data; });
    },
    getTransaction: function (transactionId) {
      return apiClient.get('kiosk/transactions/' + encodeURIComponent(transactionId)).then(function (response) { return response.data; });
    },
    startRetrieval: function (retrievalCode, faceImageBase64) {
      return apiClient.post('kiosk/retrieve/start', {
        retrievalCode: retrievalCode,
        faceImageBase64: faceImageBase64
      }).then(function (response) { return response.data; });
    },
    verifyStoreFace: function (idImage, faceImageBase64, transactionId) {
      return apiClient.post('kiosk/store/face-verify', {
        imageBase64: idImage.base64,
        filename: idImage.filename,
        contentType: idImage.contentType,
        faceImageBase64: faceImageBase64,
        transactionId: transactionId
      }).then(function (response) { return response.data; });
    },
    precheckRetrievalCode: function (retrievalCode) {
      return apiClient.post('kiosk/retrieve/lookup', { retrievalCode: retrievalCode }).then(function (response) {
        return response.data;
      });
    },
    recoverRetrieval: function (retrievalCode) {
      return apiClient.post('kiosk/retrieve/recover', { retrievalCode: retrievalCode }).then(function (response) { return response.data; });
    },
    cancelRetrieval: function (transactionId) {
      return apiClient.post('kiosk/retrieve/cancel', { transactionId: transactionId }).then(function (response) { return response.data; });
    },
    completeRetrieval: function (transactionId) {
      return apiClient.post('kiosk/retrieve/complete', { transactionId: transactionId }).then(function (response) { return response.data; });
    }
  };

  if (window.__KIOSK_TEST_MODE__ && window.__KIOSK_TEST_HOOKS__) {
    window.__KIOSK_TEST_HOOKS__.KioskTransactionService = KioskTransactionService;
  }

  // Mock boundaries: no payment data or physical locker API exists in Phase 1.1.
  var MockPaymentAdapter = {
    price: 2000,
    process: function () { return delay(1400).then(function () { return { success: true, mock: true }; }); }
  };
  var MockSettlementAdapter = {
    process: function (amount) {
      return delay(700).then(function () { return { success: true, mock: true, required: amount > 0, amount: amount }; });
    }
  };
  var MockLockerControlAdapter = {
    open: function (lockerNumber) {
      return delay(1800).then(function () {
        return { success: true, mock: true, lockerNumber: lockerNumber, state: 'OPEN' };
      });
    }
  };
  var MockHelpAdapter = {
    request: function () { return Promise.resolve({ success: true, mock: true }); }
  };

  function friendlyVerificationMessage(reason, source) {
    if (isImageFormatReason(reason)) {
      if (isCameraSource(source)) {
        return reason === 'IMAGE_TOO_LARGE' ?
          '촬영한 얼굴 사진이 너무 큽니다. 다시 촬영해 주세요.' :
          '카메라 촬영 이미지를 처리하지 못했습니다. 다시 촬영해주세요.';
      }
      if (reason === 'UNSUPPORTED_IMAGE_FORMAT') {
        return '선택한 신분증 사진을 사용할 수 없습니다. 다른 사진을 선택해 주세요.';
      }
      if (reason === 'IMAGE_TOO_LARGE') {
        return '선택한 신분증 사진이 너무 큽니다. 다른 사진을 선택해 주세요.';
      }
      return '선택한 신분증 사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.';
    }
    var messages = {
      FRAME_TIMEOUT: '촬영한 얼굴 처리 시간이 초과되었습니다. 다시 촬영해 주세요.',
      INVALID_CAPTURE_STATE: '촬영 정보를 확인하지 못했습니다. 다시 촬영해 주세요.',
      INVALID_TARGET_FRAME_ID: '촬영 정보를 확인하지 못했습니다. 다시 촬영해 주세요.',
      FRAME_METADATA_INVALID: '촬영 정보를 확인하지 못했습니다. 다시 촬영해 주세요.',
      TARGET_FRAME_TOO_OLD: '촬영 정보를 확인하지 못했습니다. 다시 촬영해 주세요.',
      NO_RECENT_FRAME: '촬영된 얼굴을 확인하지 못했습니다. 다시 촬영해 주세요.',
      NO_LATEST_FRAME: '촬영된 얼굴을 확인하지 못했습니다. 다시 촬영해 주세요.',
      NO_FACE: '얼굴을 카메라 화면 안에 맞춰주세요.',
      MULTIPLE_FACES: '한 분만 카메라 앞에 서주세요.',
      FACE_TOO_SMALL: '카메라에 조금 더 가까이 와주세요.',
      FACE_OFF_CENTER: '얼굴을 화면 중앙에 맞춰주세요.',
      TOO_DARK: '얼굴이 너무 어둡습니다. 밝은 곳에서 다시 촬영해주세요.',
      TOO_BRIGHT: '얼굴이 너무 밝게 촬영되었습니다. 위치를 조정해주세요.',
      TOO_BLURRY: '사진이 흔들리거나 흐립니다. 잠시 멈춘 상태에서 다시 촬영해주세요.',
      FACE_TILTED: '고개를 바로 세우고 정면을 바라봐주세요.',
      FACE_POSE_INVALID: '카메라 정면을 바라봐주세요.',
      QUALITY_CHECK_FAILED: '촬영 상태를 확인하지 못했습니다. 다시 촬영해 주세요.',
      QUALITY_DETECTOR_UNAVAILABLE: '얼굴 촬영 시스템을 준비하지 못했습니다. 잠시 후 다시 시도해주세요.',
      NO_AVAILABLE_LOCKER: '현재 사용 가능한 사물함이 없습니다.',
      LOCKER_NOT_AVAILABLE: '방금 다른 이용자가 선택한 사물함입니다. 다른 사물함을 선택해주세요.',
      LOCKER_HOLD_FAILED: '사물함을 확보하지 못했습니다. 다른 사물함을 선택해주세요.',
      LOCKER_HOLD_EXPIRED: '선택한 사물함의 예약 시간이 만료되었습니다. 다시 선택해주세요.',
      LOCKER_HOLD_NOT_OWNED: '선택한 사물함의 예약 정보가 없습니다. 다시 선택해주세요.',
      FACE_INDEX_FAILED: '얼굴 인증 정보를 등록하지 못했습니다. 잠시 후 다시 시도해주세요.',
      FACE_ID_MISSING: '얼굴 인증 정보를 등록하지 못했습니다. 잠시 후 다시 시도해주세요.',
      FACE_COLLECTION_NOT_CONFIGURED: '인증 서비스 설정에 문제가 있습니다. 역무원에게 도움을 요청해 주세요.',
      FACE_SEARCH_FAILED: '본인 확인 중 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요.',
      EXPECTED_FACE_NOT_FOUND: '보관 당시 사용자와 일치하지 않습니다.',
      FACE_DELETE_FAILED: '이용은 완료되었습니다. 정리 작업은 나중에 다시 시도합니다.',
      NO_FACE_IN_SOURCE_OR_TARGET: '얼굴을 인식하지 못했습니다. 얼굴이 잘 보이도록 다시 촬영해 주세요.',
      NO_FACE_IN_REFERENCE: '보관 시 등록된 얼굴을 확인하지 못했습니다. 역무원에게 도움을 요청해 주세요.',
      NO_FACE_IN_TARGET: '얼굴을 인식하지 못했습니다. 얼굴이 잘 보이도록 다시 촬영해 주세요.',
      SIMILARITY_BELOW_THRESHOLD: '등록된 얼굴과 촬영한 얼굴이 일치하지 않습니다. 다시 촬영해 주세요.',
      THROTTLED: '요청이 많습니다. 잠시 후 다시 시도해 주세요.',
      RETRIEVAL_RATE_LIMITED: '요청이 많습니다. 잠시 후 다시 시도해 주세요.',
      ACCESS_DENIED: '인증 서비스에 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.',
      AWS_API_ERROR: '인증 서비스에 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.',
      INVALID_S3_OBJECT: '촬영된 얼굴을 확인하지 못했습니다. 다시 촬영해 주세요.',
      REFERENCE_FACE_MISSING: '보관 시 등록된 얼굴 정보가 없습니다. 역무원에게 도움을 요청해 주세요.',
      INVALID_RETRIEVAL_CODE: '보관번호를 확인하지 못했습니다. 보관번호를 다시 확인해 주세요.',
      RETRIEVAL_UNAVAILABLE: '보관 정보를 확인하지 못했습니다. 보관번호를 다시 확인해 주세요.',
      SERVICE_ERROR: '인증 서비스에 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.'
    };
    return messages[reason] || '본인 확인을 완료하지 못했습니다. 얼굴이 잘 보이도록 다시 촬영해 주세요.';
  }

  new Vue({
    el: '#kiosk-app',
    data: {
      authenticated: null,
      username: '',
      password: '',
      loggingIn: false,
      loginError: '',
      state: KIOSK_STATES.ATTRACT,
      selectedMode: null,
      consentGiven: false,
      idImage: null,
      idError: '',
      idReadGeneration: 0,
      mediaStream: null,
      cameraStartPromise: null,
      cameraRequestId: 0,
      cameraStarting: false,
      cameraBusy: false,
      cameraError: '',
      capturedFaceBase64: '',
      capturedFacePreview: '',
      verificationAttempts: 0,
      verificationPassed: false,
      verificationMessage: '',
      lastFailureKind: '',
      lockers: [],
      selectedLocker: null,
      lockerError: '',
      transactionId: null,
      activeTransactionKind: null,
      reservationExpiresAt: null,
      retrievalCodeInput: '',
      retrievalCode: '',
      retrievalError: '',
      retrievalBusy: false,
      additionalFee: 0,
      mockPaymentId: '',
      completedAmount: 0,
      paymentBusy: false,
      recoveryBusy: false,
      recoveryMessage: '',
      recoveryAction: null,
      convenienceMode: false,
      voiceEnabled: false,
      idleTimer: null,
      completionTimer: null,
      operationId: 0
    },
    computed: {
      showProgress: function () {
        return [KIOSK_STATES.ATTRACT, KIOSK_STATES.MODE_SELECT, KIOSK_STATES.HELP].indexOf(this.state) === -1;
      },
      progressSteps: function () {
        var labels = this.selectedMode === 'RETRIEVE' ?
          ['보관번호', '얼굴 확인', '찾기'] :
          ['보관함 선택', '본인 인증', '결제', '보관'];
        return labels.map(function (label, index) { return { number: index + 1, label: label }; });
      },
      currentProgress: function () {
        if (this.selectedMode === 'RETRIEVE') {
          if (this.state === KIOSK_STATES.RETRIEVAL_CODE) { return 1; }
          if ([KIOSK_STATES.FACE_CAPTURE, KIOSK_STATES.WAITING_FOR_FRAME,
            KIOSK_STATES.VERIFYING, KIOSK_STATES.FACE_RETRY].indexOf(this.state) !== -1) {
            return 2;
          }
          return 3;
        }
        if ([KIOSK_STATES.LOCKER_SELECT, KIOSK_STATES.LOCKER_RESERVING].indexOf(this.state) !== -1) { return 1; }
        if ([KIOSK_STATES.CONSENT, KIOSK_STATES.ID_CAPTURE, KIOSK_STATES.FACE_CAPTURE,
          KIOSK_STATES.WAITING_FOR_FRAME, KIOSK_STATES.VERIFYING, KIOSK_STATES.FACE_RETRY].indexOf(this.state) !== -1) { return 2; }
        if ([KIOSK_STATES.PAYMENT, KIOSK_STATES.STORE_COMPLETING].indexOf(this.state) !== -1) { return 3; }
        return 4;
      },
      formattedPrice: function () {
        return MockPaymentAdapter.price.toLocaleString('ko-KR') + '원';
      },
      formattedAdditionalFee: function () {
        return Number(this.additionalFee || 0).toLocaleString('ko-KR') + '원';
      }
    },
    created: function () { this.checkAuthentication(); },
    mounted: function () {
      document.addEventListener('keydown', this.refreshIdleTimer);
      document.addEventListener('pointerdown', this.refreshIdleTimer);
      window.addEventListener('beforeunload', this.handleBeforeUnload);
    },
    beforeDestroy: function () {
      document.removeEventListener('keydown', this.refreshIdleTimer);
      document.removeEventListener('pointerdown', this.refreshIdleTimer);
      window.removeEventListener('beforeunload', this.handleBeforeUnload);
      this.stopCamera();
      this.clearAllTimers();
    },
    methods: {
      checkAuthentication: function () {
        var self = this;
        apiClient.get('me').then(function (response) {
          self.authenticated = !!(response.data && response.data.authenticated);
          if (self.authenticated) { self.resetKiosk(); }
        }).catch(function () { self.authenticated = false; });
      },
      login: function () {
        var self = this;
        if (this.loggingIn) { return; }
        this.loggingIn = true;
        this.loginError = '';
        // Confirm the HttpOnly session cookie is actually accepted before kiosk use.
        apiClient.post('login', { username: this.username, password: this.password }).then(function () {
          self.password = '';
          return apiClient.get('me').then(function (me) {
            if (!(me.data && me.data.authenticated)) {
              var err = new Error('session cookie not accepted');
              err.code = 'SESSION_COOKIE_NOT_ACCEPTED';
              throw err;
            }
            self.authenticated = true;
            self.resetKiosk();
          });
        }).catch(function (error) {
          self.authenticated = false;
          if (error && error.code === 'SESSION_COOKIE_NOT_ACCEPTED') {
            self.loginError = '로그인은 성공했지만 세션을 확인하지 못했습니다. '
              + '브라우저 쿠키 또는 프록시 설정을 확인해 주세요.';
          } else {
            self.loginError = error.response && error.response.status === 401 ?
              '아이디 또는 비밀번호가 올바르지 않습니다.' :
              '로그인할 수 없습니다. 네트워크 상태를 확인해 주세요.';
          }
        }).then(function () { self.loggingIn = false; });
      },
      go: function (nextState) {
        var allowed = TRANSITIONS[this.state] || [];
        if (allowed.indexOf(nextState) === -1) {
          console.warn('Blocked kiosk transition', this.state, '->', nextState);
          return false;
        }
        if (this.state === KIOSK_STATES.FACE_CAPTURE && nextState !== KIOSK_STATES.FACE_CAPTURE) {
          this.stopCamera();
        }
        this.state = nextState;
        this.refreshIdleTimer();
        this.onStateEntered(nextState);
        return true;
      },
      onStateEntered: function (state) {
        var self = this;
        if (state === KIOSK_STATES.FACE_CAPTURE) {
          this.capturedFaceBase64 = '';
          this.capturedFacePreview = '';
          this.cameraError = '';
          this.$nextTick(function () { self.startCamera(); });
        } else if (state === KIOSK_STATES.LOCKER_SELECT) {
          this.loadLockers();
        }
        this.speakStateInstruction(state);
      },
      beginTransaction: function () {
        if (this.state === KIOSK_STATES.ATTRACT) { this.go(KIOSK_STATES.MODE_SELECT); }
      },
      selectMode: function (mode) {
        this.selectedMode = mode;
        if (mode === 'RETRIEVE') {
          // RETRIEVE: code → current face → open locker (no ID re-check).
          this.go(KIOSK_STATES.RETRIEVAL_CODE);
        } else {
          this.go(KIOSK_STATES.LOCKER_SELECT);
        }
      },
      continueFromConsent: function () {
        if (this.consentGiven) { this.go(KIOSK_STATES.ID_CAPTURE); }
      },
      handleIdFile: function (event) {
        var self = this;
        var input = event.target;
        var file = captureSelectedFile(input);
        if (!file) {
          logIdFile('selected', { result: 'FAIL', code: 'NO_FILE' });
          return;
        }
        var generation = this.idReadGeneration + 1;
        this.idReadGeneration = generation;
        this.idError = '';
        this.idImage = null;
        var readPromise = DemoIdAdapter.read(file);
        resetFileInput(input);
        readPromise.then(function (image) {
          if (!shouldApplyIdRead(generation, self.idReadGeneration)) {
            logIdFile('stale', { result: 'IGNORE', generation: generation });
            return;
          }
          self.idImage = image;
          logIdFile('state', { result: 'PASS', generation: generation });
        }).catch(function (error) {
          if (!shouldApplyIdRead(generation, self.idReadGeneration)) {
            logIdFile('stale', { result: 'IGNORE', generation: generation });
            return;
          }
          self.idError = (error && error.message) ||
            '사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.';
          logIdFile('state', {
            result: 'FAIL',
            generation: generation,
            code: error && error.code
          });
        });
      },
      retryIdCapture: function () {
        this.idReadGeneration += 1;
        this.idImage = null;
        this.idError = '';
        this.lastFailureKind = 'id';
        this.verificationMessage = this.verificationMessage ||
          friendlyVerificationMessage('UNSUPPORTED_IMAGE_FORMAT', 'id_image');
        this.go(KIOSK_STATES.ID_CAPTURE);
      },
      startCamera: function () {
        var self = this;
        if (this.state !== KIOSK_STATES.FACE_CAPTURE || this.mediaStream || this.cameraStartPromise) {
          return this.cameraStartPromise || Promise.resolve(this.mediaStream);
        }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          this.cameraError = '이 기기에서 카메라를 사용할 수 없습니다. 역무원에게 도움을 요청해 주세요.';
          return Promise.resolve(null);
        }
        this.cameraStarting = true;
        this.cameraError = '';
        var requestId = ++this.cameraRequestId;
        var request = navigator.mediaDevices.getUserMedia({
          video: { facingMode: 'user', width: { ideal: 1080 }, height: { ideal: 1440 } },
          audio: false
        }).then(function (stream) {
          if (requestId !== self.cameraRequestId || self.state !== KIOSK_STATES.FACE_CAPTURE) {
            stream.getTracks().forEach(function (track) { track.stop(); });
            return null;
          }
          self.mediaStream = stream;
          var video = self.$refs.cameraVideo;
          if (video) {
            video.srcObject = stream;
            var playPromise = video.play();
            if (playPromise && playPromise.catch) { playPromise.catch(function () {}); }
          }
          return stream;
        }).catch(function (error) {
          if (requestId === self.cameraRequestId && self.state === KIOSK_STATES.FACE_CAPTURE) {
            self.cameraError = error && error.name === 'NotAllowedError' ?
              '카메라 사용 권한이 필요합니다. 권한을 허용하거나 도움을 요청해 주세요.' :
              '카메라를 시작하지 못했습니다. 다시 시도하거나 도움을 요청해 주세요.';
          }
          return null;
        });
        this.cameraStartPromise = request;
        request.then(function () {
          if (requestId === self.cameraRequestId) {
            self.cameraStarting = false;
            self.cameraStartPromise = null;
          }
        });
        return request;
      },
      stopCamera: function () {
        this.cameraRequestId += 1;
        this.cameraStartPromise = null;
        if (this.mediaStream) {
          this.mediaStream.getTracks().forEach(function (track) { track.stop(); });
          this.mediaStream = null;
        }
        var video = this.$refs.cameraVideo;
        if (video) { video.srcObject = null; }
        this.cameraStarting = false;
        this.cameraBusy = false;
      },
      restartCamera: function () {
        if (this.cameraBusy) { return; }
        this.stopCamera();
        this.cameraError = '';
        this.startCamera();
      },
      captureFace: function () {
        if (this.cameraBusy || this.cameraStarting || !this.mediaStream) { return; }
        var video = this.$refs.cameraVideo;
        if (!video || !video.videoWidth || !video.videoHeight) {
          this.cameraError = '카메라 화면을 준비하는 중입니다. 잠시 후 다시 눌러 주세요.';
          return;
        }
        this.cameraBusy = true;
        var canvas = document.createElement('canvas');
        var scale = Math.min(1, 960 / video.videoWidth, 1280 / video.videoHeight);
        canvas.width = Math.round(video.videoWidth * scale);
        canvas.height = Math.round(video.videoHeight * scale);
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
        var dataUrl;
        try {
          dataUrl = canvas.toDataURL('image/jpeg', 0.82);
        } catch (error) {
          this.cameraBusy = false;
          this.cameraError = '얼굴 사진을 촬영하지 못했습니다. 다시 시도해 주세요.';
          return;
        }
        this.capturedFacePreview = dataUrl;
        this.capturedFaceBase64 = dataUrl.split(',')[1];
        var operationId = ++this.operationId;
        var self = this;
        this.stopCamera();
        this.go(KIOSK_STATES.WAITING_FOR_FRAME);
        var isRetrieve = this.selectedMode === 'RETRIEVE';
        var comparePromise = isRetrieve ?
          pipelineService.waitForRetrievalComparison(
            self.retrievalCodeInput,
            self.capturedFaceBase64,
            function () { return self.operationId !== operationId; }
          ) :
          pipelineService.waitForExactComparison(
            self.idImage,
            self.capturedFaceBase64,
            function () { return self.operationId !== operationId; },
            self.transactionId
          );
        comparePromise.then(function (result) {
          if (self.operationId !== operationId) { return; }
          var reason = (result && result.reason) || 'SERVICE_ERROR';
          var source = result && result.source;
          if (reason === 'LOCKER_HOLD_EXPIRED' || reason === 'LOCKER_HOLD_NOT_OWNED') {
            self.handleHoldExpired(reason);
            return;
          }
          if (isIdImageSource(source) && isImageFormatReason(reason)) {
            self.handleVerificationFailure(reason, 'id', source);
            return;
          }
          var qualityFailed = isQualityGateFailure(reason, source);
          if (qualityFailed) {
            // Local recapture guidance. Do not count as a biometric attempt
            // and do not show the Rekognition "verifying" screen.
            self.handleVerificationFailure(reason, 'quality', source);
            return;
          }
          self.go(KIOSK_STATES.VERIFYING);
          if (isCompletedVerificationOutcome(reason)) {
            self.verificationAttempts += 1;
          }
          var passed = result && result.success === true && result.matched === true &&
            Number(result.similarity) >= SIMILARITY_THRESHOLD;
          if (passed) {
            self.verificationPassed = true;
            if (isRetrieve) {
              // FRAME-B is not stored after the comparison request.
              self.capturedFaceBase64 = '';
              self.capturedFacePreview = '';
              self.acceptRetrievalResult(result, operationId);
              return;
            }
            // STORE: keep verified FRAME-A in page memory until store/complete.
            self.speak('본인 인증이 완료되었습니다.');
            window.setTimeout(function () {
              if (self.operationId === operationId && self.state === KIOSK_STATES.VERIFYING) {
                self.go(KIOSK_STATES.PAYMENT);
              }
            }, 1200);
          } else {
            self.handleVerificationFailure(reason, 'auth', source);
          }
        }).catch(function (error) {
          if (self.operationId !== operationId || (error && error.code === 'CANCELLED')) { return; }
          self.handleVerificationFailure((error && error.code) || 'SERVICE_ERROR', 'auth');
        });
      },
      handleVerificationFailure: function (reason, kind, source) {
        this.capturedFaceBase64 = '';
        this.capturedFacePreview = '';
        this.verificationPassed = false;
        this.lastFailureKind = kind || (isQualityGateFailure(reason, source) ? 'quality' : 'auth');
        this.verificationMessage = friendlyVerificationMessage(reason, source);
        // Quality-gate recapture and frame timeouts do not consume an attempt.
        if (this.lastFailureKind !== 'quality' &&
            this.verificationAttempts >= MAX_VERIFICATION_ATTEMPTS) {
          this.requestHelp();
        } else if (this.state === KIOSK_STATES.WAITING_FOR_FRAME || this.state === KIOSK_STATES.VERIFYING) {
          this.go(KIOSK_STATES.FACE_RETRY);
        }
      },
      handleHoldExpired: function (reason) {
        var self = this;
        this.capturedFaceBase64 = '';
        this.capturedFacePreview = '';
        this.verificationPassed = false;
        this.lastFailureKind = 'hold';
        this.lockerError = friendlyVerificationMessage(reason);
        this.operationId += 1;
        this.stopCamera();
        this.cancelActiveTransactionSafely().then(function () {
          self.clearActiveTransaction();
          self.selectedLocker = null;
          if (self.state !== KIOSK_STATES.LOCKER_SELECT) {
            self.go(KIOSK_STATES.LOCKER_SELECT);
          } else {
            self.loadLockers();
          }
        }).catch(function () {
          self.clearActiveTransaction();
          self.selectedLocker = null;
          if (self.state !== KIOSK_STATES.LOCKER_SELECT) {
            self.go(KIOSK_STATES.LOCKER_SELECT);
          }
        });
      },
      returnToModeSelect: function () {
        if (this.transactionId) { return; }
        this.selectedLocker = null;
        this.lockerError = '';
        this.go(KIOSK_STATES.MODE_SELECT);
      },
      returnToLockerSelectFromConsent: function () {
        var self = this;
        if (!this.transactionId) {
          this.go(KIOSK_STATES.LOCKER_SELECT);
          return;
        }
        this.cancelActiveTransactionSafely().then(function () {
          self.clearActiveTransaction();
          self.selectedLocker = null;
          self.consentGiven = false;
          self.go(KIOSK_STATES.LOCKER_SELECT);
        }).catch(function (error) {
          self.handleCancellationFailure(error, 'CANCEL_STORE');
        });
      },
      cancelToFaceCapture: function () {
        this.operationId += 1;
        this.capturedFaceBase64 = '';
        this.capturedFacePreview = '';
        this.go(KIOSK_STATES.FACE_CAPTURE);
      },
      loadLockers: function () {
        var self = this;
        return KioskTransactionService.listLockers().then(function (lockers) {
          self.lockers = lockers;
          if (self.selectedLocker) {
            var current = lockers.find(function (locker) { return locker.lockerId === self.selectedLocker.lockerId; });
            if (!current || current.status !== 'AVAILABLE') { self.selectedLocker = null; }
          }
          var available = lockers.filter(function (locker) { return locker.status === 'AVAILABLE'; });
          if (!available.length && !self.lockerError) {
            self.lockerError = friendlyVerificationMessage('NO_AVAILABLE_LOCKER');
          }
        }).catch(function () {
          self.lockerError = '보관함 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.';
        });
      },
      selectLocker: function (locker) {
        if (locker.status === 'AVAILABLE') {
          this.selectedLocker = locker;
          this.lockerError = '';
        }
      },
      reserveSelectedLocker: function () {
        var self = this;
        if (!this.selectedLocker || this.state !== KIOSK_STATES.LOCKER_SELECT) { return; }
        var lockerId = this.selectedLocker.lockerId;
        var operationId = this.operationId;
        this.lockerError = '';
        this.go(KIOSK_STATES.LOCKER_RESERVING);
        KioskTransactionService.reserveLocker(lockerId).then(function (result) {
          if (self.operationId !== operationId) {
            KioskTransactionService.cancelStore(result.transactionId).catch(function () {});
            return;
          }
          self.transactionId = result.transactionId;
          self.activeTransactionKind = 'STORE_RESERVED';
          self.reservationExpiresAt = result.reservationExpiresAt;
          self.selectedLocker = { lockerId: result.lockerId, status: 'RESERVED' };
          self.go(KIOSK_STATES.CONSENT);
        }).catch(function (error) {
          if (self.operationId !== operationId) { return; }
          self.selectedLocker = null;
          self.go(KIOSK_STATES.LOCKER_SELECT);
          self.lockerError = error.response && error.response.status === 409 ?
            friendlyVerificationMessage('LOCKER_NOT_AVAILABLE') :
            friendlyVerificationMessage('LOCKER_HOLD_FAILED');
          self.loadLockers();
        });
      },
      cancelStoreAndChooseLocker: function () {
        var self = this;
        if (this.paymentBusy || !this.transactionId) { return; }
        this.paymentBusy = true;
        this.cancelActiveTransactionSafely().then(function () {
          self.paymentBusy = false;
          self.clearRecovery();
          self.clearActiveTransaction();
          self.selectedLocker = null;
          self.go(KIOSK_STATES.LOCKER_SELECT);
        }).catch(function (error) {
          self.paymentBusy = false;
          self.handleCancellationFailure(error, 'CANCEL_STORE');
        });
      },
      pay: function () {
        var self = this;
        if (this.paymentBusy || !this.transactionId || !this.selectedLocker) { return; }
        this.paymentBusy = true;
        var operationId = this.operationId;
        MockPaymentAdapter.process().then(function (result) {
          if (self.operationId !== operationId || !result.success) { return null; }
          self.go(KIOSK_STATES.STORE_COMPLETING);
          return self.completeStoreWithRecovery(operationId);
        }).catch(function () {
          if (self.operationId !== operationId) { return; }
          self.paymentBusy = false;
          self.showRecovery('거래 상태를 확인하지 못했습니다.\n잠시 후 다시 시도해 주세요.', 'STORE_COMPLETE');
        });
      },
      completeStoreWithRecovery: function (operationId) {
        var self = this;
        var transactionId = this.transactionId;
        this.paymentBusy = true;
        return KioskTransactionService.completeStore(
          transactionId,
          this.capturedFaceBase64
        ).catch(function (error) {
          var completeReason = httpErrorDetail(error);
          if (isKnownStoreCompleteFailure(completeReason)) {
            self.paymentBusy = false;
            self.showRecovery(
              friendlyVerificationMessage(completeReason),
              'STORE_COMPLETE'
            );
            return null;
          }
          if (!isAmbiguousRequestError(error)) { throw error; }
          self.recoveryBusy = true;
          self.recoveryMessage = '거래 상태를 확인하고 있습니다...';
          self.recoveryAction = null;
          return KioskTransactionService.getTransaction(transactionId).then(function (status) {
            if (status.status !== 'STORED') { throw new Error('store completion not confirmed'); }
            return status;
          });
        }).then(function (result) {
          if (!result || self.operationId !== operationId) { return null; }
          self.clearRecovery();
          self.paymentBusy = false;
          self.retrievalCode = result.retrievalCode;
          self.mockPaymentId = result.mockPaymentId;
          self.completedAmount = result.amount;
          self.selectedLocker = { lockerId: result.lockerId, status: 'OCCUPIED' };
          self.activeTransactionKind = null;
          self.reservationExpiresAt = null;
          self.capturedFaceBase64 = '';
          self.capturedFacePreview = '';
          if (!self.go(KIOSK_STATES.LOCKER_OPENING)) { return null; }
          return MockLockerControlAdapter.open(result.lockerId);
        }).then(function (result) {
          if (!result || self.operationId !== operationId || self.state !== KIOSK_STATES.LOCKER_OPENING) { return; }
          self.finishTransaction();
        });
      },
      retryStoreCompletion: function () {
        if (!this.transactionId || this.paymentBusy) { return; }
        this.clearRecovery();
        var self = this;
        var operationId = this.operationId;
        this.completeStoreWithRecovery(operationId).catch(function () {
          if (self.operationId !== operationId) { return; }
          self.paymentBusy = false;
          self.showRecovery('거래 상태를 확인하지 못했습니다.\n잠시 후 다시 시도해 주세요.', 'STORE_COMPLETE');
        });
      },
      normalizeRetrievalCode: function () {
        this.retrievalCodeInput = String(this.retrievalCodeInput || '').replace(/\D/g, '').slice(0, 8);
        this.retrievalError = '';
      },
      continueFromRetrievalCode: function () {
        if (this.retrievalBusy || this.retrievalCodeInput.length !== 8) { return; }
        if (!/^\d{8}$/.test(this.retrievalCodeInput)) {
          this.retrievalError = friendlyVerificationMessage('INVALID_RETRIEVAL_CODE');
          return;
        }
        var self = this;
        this.retrievalBusy = true;
        this.retrievalError = '';
        KioskTransactionService.precheckRetrievalCode(this.retrievalCodeInput).then(function (result) {
          if (self.state !== KIOSK_STATES.RETRIEVAL_CODE) { return; }
          self.retrievalBusy = false;
          if (!result || result.ready !== true) {
            self.retrievalError = friendlyVerificationMessage('RETRIEVAL_UNAVAILABLE');
            return;
          }
          self.verificationAttempts = 0;
          self.verificationPassed = false;
          self.verificationMessage = '';
          self.lastFailureKind = '';
          self.go(KIOSK_STATES.FACE_CAPTURE);
        }).catch(function (error) {
          if (self.state !== KIOSK_STATES.RETRIEVAL_CODE) { return; }
          self.retrievalBusy = false;
          var detail = httpErrorDetail(error);
          if (error && error.response && error.response.status === 429) {
            self.retrievalError = friendlyVerificationMessage('RETRIEVAL_RATE_LIMITED');
            return;
          }
          self.retrievalError = friendlyVerificationMessage(detail || 'RETRIEVAL_UNAVAILABLE');
        });
      },
      // Legacy name retained; face-gated start now happens after capture.
      startRetrieval: function () {
        this.continueFromRetrievalCode();
      },
      acceptRetrievalResult: function (result, operationId) {
        var self = this;
        if (this.operationId !== operationId) {
          if (result && result.transactionId) {
            KioskTransactionService.cancelRetrieval(result.transactionId).catch(function () {});
          }
          return;
        }
        if (!result || !result.transactionId || !result.lockerId) {
          this.handleVerificationFailure((result && result.reason) || 'SERVICE_ERROR');
          return;
        }
        this.clearRecovery();
        this.retrievalBusy = false;
        this.transactionId = result.transactionId;
        this.activeTransactionKind = 'RETRIEVING';
        this.selectedLocker = { lockerId: result.lockerId, status: 'OCCUPIED' };
        this.additionalFee = Number(result.additionalFee || 0);
        this.speak('본인 확인이 완료되었습니다. 보관함을 엽니다.');
        if (!this.go(KIOSK_STATES.LOCKER_OPENING)) { return; }
        MockLockerControlAdapter.open(result.lockerId).then(function (opened) {
          if (!opened || self.operationId !== operationId) { return null; }
          return self.completeRetrievalWithRecovery(operationId);
        }).catch(function () {
          if (self.operationId !== operationId) { return; }
          self.showRecovery('거래 상태를 확인하지 못했습니다.\n잠시 후 다시 시도해 주세요.', 'RETRIEVE_COMPLETE');
        });
      },
      recoverRetrievalStart: function (operationId) {
        // Face verification is required to re-enter RETRIEVING; recover only
        // resumes an already-authorized RETRIEVING lease.
        var self = this;
        var retrievalCode = this.retrievalCodeInput;
        this.recoveryBusy = true;
        this.recoveryMessage = '거래 상태를 확인하고 있습니다...';
        this.recoveryAction = null;
        KioskTransactionService.recoverRetrieval(retrievalCode).then(function (result) {
          if (result.status === 'STORED') {
            self.clearRecovery();
            self.retrievalBusy = false;
            self.retrievalError = '얼굴 확인 후 다시 시도해 주세요.';
            self.go(KIOSK_STATES.FACE_CAPTURE);
            return null;
          }
          return result;
        }).then(function (result) {
          if (!result) { return; }
          self.acceptRetrievalResult(result, operationId);
        }).catch(function (error) {
          if (self.operationId !== operationId) { return; }
          self.retrievalBusy = false;
          if (error.response && error.response.status === 429) {
            self.clearRecovery();
            self.retrievalError = '보관번호 확인 요청이 많습니다. 잠시 후 다시 시도해 주세요.';
            return;
          }
          self.showRecovery('거래 상태를 확인하지 못했습니다.\n잠시 후 다시 시도해 주세요.', 'RETRIEVE_START');
        });
      },
      cancelRetrievalAndReturn: function () {
        var self = this;
        if (this.retrievalBusy || !this.transactionId) { return; }
        this.retrievalBusy = true;
        this.cancelActiveTransactionSafely().then(function () {
          self.retrievalBusy = false;
          self.clearRecovery();
          self.clearActiveTransaction();
          self.selectedLocker = null;
          self.additionalFee = 0;
          self.go(KIOSK_STATES.RETRIEVAL_CODE);
        }).catch(function (error) {
          self.retrievalBusy = false;
          self.handleCancellationFailure(error, 'CANCEL_RETRIEVAL');
        });
      },
      continueToSettlement: function () {
        this.go(KIOSK_STATES.RETRIEVE_SETTLEMENT);
      },
      settleAndOpen: function () {
        var self = this;
        if (this.paymentBusy || !this.transactionId || !this.selectedLocker) { return; }
        this.paymentBusy = true;
        var operationId = this.operationId;
        MockSettlementAdapter.process(this.additionalFee).then(function (result) {
          if (self.operationId !== operationId || !result.success) { return null; }
          self.paymentBusy = false;
          if (!self.go(KIOSK_STATES.LOCKER_OPENING)) { return null; }
          return MockLockerControlAdapter.open(self.selectedLocker.lockerId);
        }).then(function (result) {
          if (!result || self.operationId !== operationId || self.state !== KIOSK_STATES.LOCKER_OPENING) { return null; }
          return self.completeRetrievalWithRecovery(operationId);
        }).catch(function () {
          if (self.operationId !== operationId) { return; }
          self.paymentBusy = false;
          self.showRecovery('거래 상태를 확인하지 못했습니다.\n잠시 후 다시 시도해 주세요.', 'RETRIEVE_COMPLETE');
        });
      },
      completeRetrievalWithRecovery: function (operationId) {
        var self = this;
        var transactionId = this.transactionId;
        this.paymentBusy = true;
        return KioskTransactionService.completeRetrieval(transactionId).catch(function () {
          self.recoveryBusy = true;
          self.recoveryMessage = '거래 상태를 확인하고 있습니다...';
          self.recoveryAction = null;
          return KioskTransactionService.getTransaction(transactionId).then(function (status) {
            if (status.status !== 'RETRIEVED') { throw new Error('retrieval completion not confirmed'); }
            return status;
          });
        }).then(function (result) {
          if (!result || self.operationId !== operationId) { return; }
          self.clearRecovery();
          self.paymentBusy = false;
          self.activeTransactionKind = null;
          self.finishTransaction();
        });
      },
      retryRetrievalCompletion: function () {
        if (!this.transactionId || this.paymentBusy) { return; }
        this.clearRecovery();
        var self = this;
        var operationId = this.operationId;
        this.completeRetrievalWithRecovery(operationId).catch(function () {
          if (self.operationId !== operationId) { return; }
          self.paymentBusy = false;
          self.showRecovery('거래 상태를 확인하지 못했습니다.\n잠시 후 다시 시도해 주세요.', 'RETRIEVE_COMPLETE');
        });
      },
      finishTransaction: function () {
        var self = this;
        this.go(KIOSK_STATES.COMPLETE);
        this.completionTimer = window.setTimeout(function () { self.resetKiosk(); }, 12000);
      },
      clearActiveTransaction: function () {
        this.transactionId = null;
        this.activeTransactionKind = null;
        this.reservationExpiresAt = null;
      },
      cancelActiveTransaction: function (useBeacon) {
        if (!this.transactionId || !this.activeTransactionKind) { return Promise.resolve(); }
        var endpoint = getApiBaseUrl() + (this.activeTransactionKind === 'STORE_RESERVED' ?
          '/kiosk/store/cancel' : '/kiosk/retrieve/cancel');
        var payload = { transactionId: this.transactionId };
        if (useBeacon && navigator.sendBeacon) {
          var body = new Blob([JSON.stringify(payload)], { type: 'application/json' });
          navigator.sendBeacon(endpoint, body);
          return Promise.resolve();
        }
        return Promise.resolve();
      },
      handleBeforeUnload: function () { this.cancelActiveTransaction(true); },
      cancelActiveTransactionSafely: function () {
        var transactionId = this.transactionId;
        var kind = this.activeTransactionKind;
        if (!transactionId || !kind) { return Promise.resolve(true); }
        var cancellation = kind === 'STORE_RESERVED' ?
          KioskTransactionService.cancelStore(transactionId) :
          KioskTransactionService.cancelRetrieval(transactionId);
        function verifyStatus() {
          return KioskTransactionService.getTransaction(transactionId).then(function (status) {
            var safelyEnded = kind === 'STORE_RESERVED' ?
              ['CANCELLED', 'EXPIRED'].indexOf(status.status) !== -1 :
              ['STORED', 'RETRIEVED'].indexOf(status.status) !== -1;
            if (safelyEnded) { return true; }
            var activeError = new Error('transaction remains active');
            activeError.transactionStatus = status;
            throw activeError;
          });
        }
        return cancellation.then(function (result) {
          return result.cancelled ? true : verifyStatus();
        }, function () {
          return verifyStatus();
        });
      },
      showRecovery: function (message, action) {
        this.recoveryBusy = false;
        this.recoveryMessage = message;
        this.recoveryAction = action;
      },
      clearRecovery: function () {
        this.recoveryBusy = false;
        this.recoveryMessage = '';
        this.recoveryAction = null;
      },
      retryRecovery: function () {
        var action = this.recoveryAction;
        this.clearRecovery();
        if (action === 'CANCEL_STORE') { this.cancelStoreAndChooseLocker(); }
        else if (action === 'CANCEL_RETRIEVAL') { this.cancelRetrievalAndReturn(); }
        else if (action === 'CANCEL_RESET') { this.resetKiosk(); }
        else if (action === 'STORE_COMPLETE') { this.retryStoreCompletion(); }
        else if (action === 'RETRIEVE_START') {
          this.retrievalBusy = false;
          this.retrievalError = '보관번호를 확인한 뒤 다시 시도해 주세요.';
        } else if (action === 'RETRIEVE_COMPLETE') { this.retryRetrievalCompletion(); }
        else if (action === 'HELP_CANCEL') { this.requestHelp(); }
      },
      handleCancellationFailure: function (error, fallbackAction) {
        var status = error && error.transactionStatus;
        if (this.activeTransactionKind === 'STORE_RESERVED' && status && status.status === 'STORED') {
          var self = this;
          var operationId = this.operationId;
          this.clearRecovery();
          this.paymentBusy = false;
          this.retrievalCode = status.retrievalCode;
          this.mockPaymentId = status.mockPaymentId;
          this.completedAmount = status.amount;
          this.selectedLocker = { lockerId: status.lockerId, status: 'OCCUPIED' };
          this.activeTransactionKind = null;
          this.reservationExpiresAt = null;
          if (this.state === KIOSK_STATES.PAYMENT) { this.go(KIOSK_STATES.STORE_COMPLETING); }
          if (this.state === KIOSK_STATES.STORE_COMPLETING && this.go(KIOSK_STATES.LOCKER_OPENING)) {
            MockLockerControlAdapter.open(status.lockerId).then(function () {
              if (self.operationId === operationId && self.state === KIOSK_STATES.LOCKER_OPENING) {
                self.finishTransaction();
              }
            });
            return;
          }
        }
        this.showRecovery('거래 상태를 정리하지 못했습니다.\n잠시 후 다시 시도해 주세요.', fallbackAction);
      },
      requestHelp: function () {
        var self = this;
        this.operationId += 1;
        if (this.transactionId && this.activeTransactionKind) {
          this.recoveryBusy = true;
          this.recoveryMessage = '거래 상태를 확인하고 있습니다...';
          this.recoveryAction = null;
          this.cancelActiveTransactionSafely().then(function () {
            self.clearActiveTransaction();
            self.enterHelpState();
          }).catch(function (error) {
            self.handleCancellationFailure(error, 'HELP_CANCEL');
          });
          return;
        }
        this.enterHelpState();
      },
      enterHelpState: function () {
        this.clearRecovery();
        this.stopCamera();
        this.capturedFaceBase64 = '';
        this.capturedFacePreview = '';
        MockHelpAdapter.request();
        if (this.state !== KIOSK_STATES.HELP) { this.go(KIOSK_STATES.HELP); }
      },
      toggleConvenience: function () {
        this.convenienceMode = !this.convenienceMode;
        this.voiceEnabled = this.convenienceMode && 'speechSynthesis' in window;
        if (!this.convenienceMode && 'speechSynthesis' in window) {
          window.speechSynthesis.cancel();
        } else {
          this.speakStateInstruction(this.state);
        }
      },
      speak: function (text) {
        if (!this.voiceEnabled || !('speechSynthesis' in window) || !window.SpeechSynthesisUtterance) { return; }
        window.speechSynthesis.cancel();
        var utterance = new window.SpeechSynthesisUtterance(text);
        utterance.lang = 'ko-KR';
        utterance.rate = 0.92;
        window.speechSynthesis.speak(utterance);
      },
      speakStateInstruction: function (state) {
        var instructions = {};
        instructions[KIOSK_STATES.ATTRACT] = '화면을 터치하여 시작하세요.';
        instructions[KIOSK_STATES.MODE_SELECT] = '물품 보관 또는 물품 찾기를 선택해 주세요.';
        instructions[KIOSK_STATES.CONSENT] = '본인 확인 안내를 읽고 동의해 주세요.';
        instructions[KIOSK_STATES.ID_CAPTURE] = '데모용 본인 사진을 선택해 주세요.';
        instructions[KIOSK_STATES.FACE_CAPTURE] = '마스크와 선글라스를 벗고 정면을 바라봐 주세요.';
        instructions[KIOSK_STATES.WAITING_FOR_FRAME] = '촬영한 얼굴을 확인하고 있습니다. 잠시 기다려 주세요.';
        instructions[KIOSK_STATES.VERIFYING] = '본인 여부를 확인하고 있습니다.';
        instructions[KIOSK_STATES.FACE_RETRY] = '얼굴이 잘 보이도록 다시 촬영해 주세요.';
        instructions[KIOSK_STATES.LOCKER_SELECT] = '사용 가능한 보관함을 선택해 주세요.';
        instructions[KIOSK_STATES.LOCKER_RESERVING] = '선택한 보관함을 예약하고 있습니다.';
        instructions[KIOSK_STATES.PAYMENT] = '이용 요금을 확인하고 결제하기를 눌러 주세요.';
        instructions[KIOSK_STATES.STORE_COMPLETING] = '보관 정보를 만들고 있습니다.';
        instructions[KIOSK_STATES.RETRIEVAL_CODE] = '8자리 보관번호를 입력해 주세요.';
        instructions[KIOSK_STATES.RETRIEVE_CONFIRM] = '찾을 보관함 정보를 확인해 주세요.';
        instructions[KIOSK_STATES.RETRIEVE_SETTLEMENT] = '추가 정산 금액을 확인해 주세요.';
        if (this.selectedMode === 'RETRIEVE') {
          instructions[KIOSK_STATES.FACE_CAPTURE] = '본인 확인을 위해 얼굴을 촬영해 주세요.';
          instructions[KIOSK_STATES.VERIFYING] = '보관 시 등록한 얼굴과 비교하고 있습니다.';
        }
        instructions[KIOSK_STATES.LOCKER_OPENING] = '보관함을 열고 있습니다.';
        instructions[KIOSK_STATES.COMPLETE] = this.selectedMode === 'STORE' ? '보관이 완료되었습니다.' : '물품 찾기가 완료되었습니다.';
        instructions[KIOSK_STATES.HELP] = '역무원에게 도움을 요청했습니다.';
        this.speak(instructions[state] || '');
      },
      refreshIdleTimer: function () {
        var self = this;
        if (!this.authenticated) { return; }
        if (this.idleTimer) { window.clearTimeout(this.idleTimer); }
        this.idleTimer = window.setTimeout(function () {
          if (self.state === KIOSK_STATES.ATTRACT) { self.refreshIdleTimer(); } else { self.resetKiosk(); }
        }, IDLE_TIMEOUT_MS);
      },
      clearAllTimers: function () {
        if (this.idleTimer) { window.clearTimeout(this.idleTimer); this.idleTimer = null; }
        if (this.completionTimer) { window.clearTimeout(this.completionTimer); this.completionTimer = null; }
      },
      resetKiosk: function () {
        var self = this;
        if (this.recoveryBusy) { return; }
        this.operationId += 1;
        if (this.transactionId && this.activeTransactionKind) {
          this.stopCamera();
          this.recoveryBusy = true;
          this.recoveryMessage = '거래 상태를 확인하고 있습니다...';
          this.recoveryAction = null;
          this.cancelActiveTransactionSafely().then(function () {
            self.performLocalReset();
          }).catch(function (error) {
            self.handleCancellationFailure(error, 'CANCEL_RESET');
          });
          return;
        }
        this.performLocalReset();
      },
      performLocalReset: function () {
        this.stopCamera();
        this.clearAllTimers();
        this.clearRecovery();
        pipelineService.clearGateway();
        if ('speechSynthesis' in window) { window.speechSynthesis.cancel(); }
        this.selectedMode = null;
        this.consentGiven = false;
        this.idReadGeneration += 1;
        this.idImage = null;
        this.idError = '';
        this.capturedFaceBase64 = '';
        this.capturedFacePreview = '';
        this.verificationAttempts = 0;
        this.verificationPassed = false;
        this.verificationMessage = '';
        this.lastFailureKind = '';
        this.lockers = [];
        this.selectedLocker = null;
        this.lockerError = '';
        this.clearActiveTransaction();
        this.retrievalCodeInput = '';
        this.retrievalCode = '';
        this.retrievalError = '';
        this.retrievalBusy = false;
        this.additionalFee = 0;
        this.mockPaymentId = '';
        this.completedAmount = 0;
        this.paymentBusy = false;
        this.cameraError = '';
        this.state = KIOSK_STATES.ATTRACT;
        this.refreshIdleTimer();
      },
      lockerStatusText: function (status) {
        return { AVAILABLE: '사용 가능', RESERVED: '예약 중', OCCUPIED: '사용 중', DISABLED: '선택 불가' }[status] || status;
      },
      lockerAriaLabel: function (locker) {
        return locker.lockerId + '번 보관함, ' + this.lockerStatusText(locker.status) +
          (this.selectedLocker && this.selectedLocker.lockerId === locker.lockerId ? ', 선택됨' : '');
      },
      twoDigits: function (number) { return String(number).padStart(2, '0'); }
    }
  });
}());
