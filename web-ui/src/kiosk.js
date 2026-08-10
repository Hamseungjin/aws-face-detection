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
    MODE_SELECT: ['CONSENT', 'ATTRACT', 'HELP'],
    CONSENT: ['ID_CAPTURE', 'ATTRACT', 'HELP'],
    ID_CAPTURE: ['FACE_CAPTURE', 'ATTRACT', 'HELP'],
    FACE_CAPTURE: ['WAITING_FOR_FRAME', 'ATTRACT', 'HELP'],
    WAITING_FOR_FRAME: ['VERIFYING', 'FACE_CAPTURE', 'FACE_RETRY', 'ATTRACT', 'HELP'],
    VERIFYING: ['LOCKER_SELECT', 'RETRIEVAL_CODE', 'FACE_RETRY', 'ATTRACT', 'HELP'],
    FACE_RETRY: ['FACE_CAPTURE', 'ATTRACT', 'HELP'],
    LOCKER_SELECT: ['LOCKER_RESERVING', 'ATTRACT', 'HELP'],
    LOCKER_RESERVING: ['LOCKER_SELECT', 'PAYMENT', 'ATTRACT', 'HELP'],
    PAYMENT: ['LOCKER_SELECT', 'STORE_COMPLETING', 'ATTRACT', 'HELP'],
    STORE_COMPLETING: ['LOCKER_OPENING', 'ATTRACT', 'HELP'],
    RETRIEVAL_CODE: ['RETRIEVE_CONFIRM', 'ATTRACT', 'HELP'],
    RETRIEVE_CONFIRM: ['RETRIEVAL_CODE', 'RETRIEVE_SETTLEMENT', 'ATTRACT', 'HELP'],
    RETRIEVE_SETTLEMENT: ['LOCKER_OPENING', 'ATTRACT', 'HELP'],
    LOCKER_OPENING: ['COMPLETE', 'ATTRACT', 'HELP'],
    COMPLETE: ['ATTRACT'],
    HELP: ['ATTRACT']
  });

  var SIMILARITY_THRESHOLD = 90;
  var MAX_ID_IMAGE_BYTES = 5 * 1024 * 1024;
  var FRAME_WAIT_TIMEOUT_MS = 15000;
  var FRAME_POLL_INTERVAL_MS = 1500;
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

  function isCompletedVerificationOutcome(reason) {
    return ['SIMILARITY_ABOVE_THRESHOLD', 'SIMILARITY_BELOW_THRESHOLD',
      'NO_FACE_IN_SOURCE_OR_TARGET'].indexOf(reason) !== -1;
  }

  // Demo boundary: replace with an approved mobile-ID or physical-ID integration.
  var DemoIdAdapter = {
    read: function (file) {
      return new Promise(function (resolve, reject) {
        if (!file) {
          reject(new Error('사진 파일을 선택해 주세요.'));
          return;
        }
        var extension = (file.name.split('.').pop() || '').toLowerCase();
        var inferredType = extension === 'png' ? 'image/png' :
          (extension === 'jpg' || extension === 'jpeg' ? 'image/jpeg' : '');
        var contentType = file.type || inferredType;
        if (['image/jpeg', 'image/png'].indexOf(contentType) === -1 || !inferredType) {
          reject(new Error('JPG, JPEG 또는 PNG 사진만 선택할 수 있습니다.'));
          return;
        }
        if (file.size > MAX_ID_IMAGE_BYTES) {
          reject(new Error('사진 크기는 5MiB 이하여야 합니다.'));
          return;
        }
        var reader = new FileReader();
        reader.onload = function (event) {
          var dataUrl = event.target.result || '';
          var separator = dataUrl.indexOf(',');
          if (separator < 0) {
            reject(new Error('사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.'));
            return;
          }
          resolve({
            filename: file.name,
            contentType: contentType,
            base64: dataUrl.slice(separator + 1),
            previewUrl: dataUrl
          });
        };
        reader.onerror = function () {
          reject(new Error('사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.'));
        };
        reader.readAsDataURL(file);
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
  PipelineService.prototype.sendCapturedFrame = function (imageBase64, frameCount) {
    return this.client.post('capture-frame', {
      imageBase64: imageBase64,
      frameCount: frameCount
    }).then(function (response) { return response.data; });
  };
  PipelineService.prototype.compareFace = function (idImage, targetFrameId) {
    return this.getGateway().then(function (gateway) {
      return gateway.post('face-compare', {
        imageBase64: idImage.base64,
        filename: idImage.filename,
        contentType: idImage.contentType,
        similarityThreshold: SIMILARITY_THRESHOLD,
        targetFrameId: targetFrameId
      });
    }).then(function (response) {
      return response.data;
    }).catch(function (error) {
      if (error.response && error.response.data && error.response.data.reason) {
        return error.response.data;
      }
      throw error;
    });
  };
  PipelineService.prototype.waitForExactComparison = function (idImage, targetFrameId, isCancelled) {
    var self = this;
    var deadline = Date.now() + FRAME_WAIT_TIMEOUT_MS;
    function attempt() {
      if (isCancelled()) { return Promise.reject(cancelledError()); }
      if (Date.now() >= deadline) {
        var timeout = new Error('processed frame timeout');
        timeout.code = 'FRAME_TIMEOUT';
        return Promise.reject(timeout);
      }
      return self.compareFace(idImage, targetFrameId).then(function (result) {
        if (!result || result.reason !== 'TARGET_FRAME_NOT_READY') { return result; }
        // Async processing retries always reuse this exact immutable capture id.
        return delay(FRAME_POLL_INTERVAL_MS).then(attempt);
      });
    }
    return attempt();
  };

  var pipelineService = new PipelineService(apiClient);

  // Opt-in unit-test seam; absent during normal kiosk operation.
  if (window.__KIOSK_TEST_MODE__) {
    window.__KIOSK_TEST_HOOKS__ = {
      PipelineService: PipelineService,
      isCompletedVerificationOutcome: isCompletedVerificationOutcome,
      getApiBaseUrl: getApiBaseUrl
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
    completeStore: function (transactionId) {
      return apiClient.post('kiosk/store/complete', { transactionId: transactionId }).then(function (response) { return response.data; });
    },
    getTransaction: function (transactionId) {
      return apiClient.get('kiosk/transactions/' + encodeURIComponent(transactionId)).then(function (response) { return response.data; });
    },
    startRetrieval: function (retrievalCode) {
      return apiClient.post('kiosk/retrieve/start', { retrievalCode: retrievalCode }).then(function (response) { return response.data; });
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

  function friendlyVerificationMessage(reason) {
    var messages = {
      FRAME_TIMEOUT: '촬영한 얼굴 처리 시간이 초과되었습니다. 다시 촬영해 주세요.',
      INVALID_CAPTURE_STATE: '촬영 정보를 확인하지 못했습니다. 다시 촬영해 주세요.',
      INVALID_TARGET_FRAME_ID: '촬영 정보를 확인하지 못했습니다. 다시 촬영해 주세요.',
      FRAME_METADATA_INVALID: '촬영 정보를 확인하지 못했습니다. 다시 촬영해 주세요.',
      TARGET_FRAME_TOO_OLD: '촬영 정보를 확인하지 못했습니다. 다시 촬영해 주세요.',
      NO_RECENT_FRAME: '촬영된 얼굴을 확인하지 못했습니다. 다시 촬영해 주세요.',
      NO_LATEST_FRAME: '촬영된 얼굴을 확인하지 못했습니다. 다시 촬영해 주세요.',
      NO_FACE_IN_SOURCE_OR_TARGET: '얼굴을 인식하지 못했습니다. 얼굴이 잘 보이도록 다시 촬영해 주세요.',
      SIMILARITY_BELOW_THRESHOLD: '신분증 사진과 촬영한 얼굴이 일치하지 않습니다. 다시 촬영해 주세요.',
      THROTTLED: '요청이 많습니다. 잠시 후 다시 시도해 주세요.',
      ACCESS_DENIED: '인증 서비스에 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.',
      AWS_API_ERROR: '인증 서비스에 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.',
      INVALID_S3_OBJECT: '촬영된 얼굴을 확인하지 못했습니다. 다시 촬영해 주세요.',
      UNSUPPORTED_IMAGE_FORMAT: '선택한 신분증 사진을 사용할 수 없습니다. 다른 사진을 선택해 주세요.',
      IMAGE_TOO_LARGE: '선택한 신분증 사진이 너무 큽니다. 다른 사진을 선택해 주세요.',
      INVALID_IMAGE: '선택한 신분증 사진을 읽지 못했습니다. 다른 사진을 선택해 주세요.',
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
      mediaStream: null,
      cameraStartPromise: null,
      cameraRequestId: 0,
      cameraStarting: false,
      cameraBusy: false,
      cameraError: '',
      capturedFaceBase64: '',
      capturedFacePreview: '',
      currentCaptureId: null,
      verificationAttempts: 0,
      verificationPassed: false,
      verificationMessage: '',
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
          ['본인 인증', '보관번호 확인', '정산', '찾기'] :
          ['본인 인증', '보관함 선택', '결제', '보관'];
        return labels.map(function (label, index) { return { number: index + 1, label: label }; });
      },
      currentProgress: function () {
        if ([KIOSK_STATES.CONSENT, KIOSK_STATES.ID_CAPTURE, KIOSK_STATES.FACE_CAPTURE,
          KIOSK_STATES.WAITING_FOR_FRAME, KIOSK_STATES.VERIFYING, KIOSK_STATES.FACE_RETRY].indexOf(this.state) !== -1) { return 1; }
        if ([KIOSK_STATES.LOCKER_SELECT, KIOSK_STATES.LOCKER_RESERVING,
          KIOSK_STATES.RETRIEVAL_CODE, KIOSK_STATES.RETRIEVE_CONFIRM].indexOf(this.state) !== -1) { return 2; }
        if ([KIOSK_STATES.PAYMENT, KIOSK_STATES.STORE_COMPLETING,
          KIOSK_STATES.RETRIEVE_SETTLEMENT].indexOf(this.state) !== -1) { return 3; }
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
        apiClient.post('login', { username: this.username, password: this.password }).then(function () {
          self.password = '';
          self.authenticated = true;
          self.resetKiosk();
        }).catch(function (error) {
          self.loginError = error.response && error.response.status === 401 ?
            '아이디 또는 비밀번호가 올바르지 않습니다.' :
            '로그인할 수 없습니다. 네트워크 상태를 확인해 주세요.';
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
          this.currentCaptureId = null;
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
        this.go(KIOSK_STATES.CONSENT);
      },
      continueFromConsent: function () {
        if (this.consentGiven) { this.go(KIOSK_STATES.ID_CAPTURE); }
      },
      handleIdFile: function (event) {
        var self = this;
        var input = event.target;
        this.idError = '';
        this.idImage = null;
        DemoIdAdapter.read(input.files && input.files[0]).then(function (image) {
          self.idImage = image;
        }).catch(function (error) {
          self.idError = error.message;
          input.value = '';
        });
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
        var frameCount = Math.floor(Date.now() % 2147483647);
        var self = this;
        this.stopCamera();
        this.go(KIOSK_STATES.WAITING_FOR_FRAME);
        pipelineService.sendCapturedFrame(this.capturedFaceBase64, frameCount).then(function (capture) {
          if (self.operationId !== operationId) { throw cancelledError(); }
          if (!capture || typeof capture.captureId !== 'string' || !capture.captureId) {
            var invalidCapture = new Error('capture id missing');
            invalidCapture.code = 'INVALID_CAPTURE_STATE';
            throw invalidCapture;
          }
          self.currentCaptureId = capture.captureId;
          self.capturedFaceBase64 = '';
          self.capturedFacePreview = '';
          return pipelineService.waitForExactComparison(
            self.idImage,
            self.currentCaptureId,
            function () { return self.operationId !== operationId; }
          );
        }).then(function (result) {
          if (self.operationId !== operationId) { return; }
          self.go(KIOSK_STATES.VERIFYING);
          // Async TARGET_FRAME_NOT_READY polling never consumes an attempt. Count
          // only a completed Rekognition verification outcome.
          if (isCompletedVerificationOutcome(result && result.reason)) {
            self.verificationAttempts += 1;
          }
          var passed = result && result.success === true && result.matched === true &&
            Number(result.similarity) >= SIMILARITY_THRESHOLD;
          if (passed) {
            self.verificationPassed = true;
            self.currentCaptureId = null;
            self.speak('본인 인증이 완료되었습니다.');
            window.setTimeout(function () {
              if (self.operationId === operationId && self.state === KIOSK_STATES.VERIFYING) {
                self.go(self.selectedMode === 'STORE' ? KIOSK_STATES.LOCKER_SELECT : KIOSK_STATES.RETRIEVAL_CODE);
              }
            }, 1200);
          } else {
            self.handleVerificationFailure((result && result.reason) || 'SERVICE_ERROR');
          }
        }).catch(function (error) {
          if (self.operationId !== operationId || (error && error.code === 'CANCELLED')) { return; }
          self.handleVerificationFailure((error && error.code) || 'SERVICE_ERROR');
        });
      },
      handleVerificationFailure: function (reason) {
        this.capturedFaceBase64 = '';
        this.capturedFacePreview = '';
        this.currentCaptureId = null;
        this.verificationPassed = false;
        this.verificationMessage = friendlyVerificationMessage(reason);
        // A frame timeout happens before compareFace and therefore consumes no attempt.
        if (this.verificationAttempts >= MAX_VERIFICATION_ATTEMPTS) {
          this.requestHelp();
        } else if (this.state === KIOSK_STATES.WAITING_FOR_FRAME || this.state === KIOSK_STATES.VERIFYING) {
          this.go(KIOSK_STATES.FACE_RETRY);
        }
      },
      cancelToFaceCapture: function () {
        this.operationId += 1;
        this.capturedFaceBase64 = '';
        this.capturedFacePreview = '';
        this.currentCaptureId = null;
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
          self.go(KIOSK_STATES.PAYMENT);
        }).catch(function (error) {
          if (self.operationId !== operationId) { return; }
          self.selectedLocker = null;
          self.go(KIOSK_STATES.LOCKER_SELECT);
          self.lockerError = error.response && error.response.status === 409 ?
            '다른 사용자가 먼저 선택한 보관함입니다. 다른 보관함을 선택해 주세요.' :
            '보관함을 예약하지 못했습니다. 잠시 후 다시 시도해 주세요.';
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
        return KioskTransactionService.completeStore(transactionId).catch(function (error) {
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
      startRetrieval: function () {
        var self = this;
        if (this.retrievalBusy || !this.retrievalCodeInput) { return; }
        this.retrievalBusy = true;
        this.retrievalError = '';
        var operationId = this.operationId;
        KioskTransactionService.startRetrieval(this.retrievalCodeInput).then(function (result) {
          self.acceptRetrievalResult(result, operationId);
        }).catch(function (error) {
          if (self.operationId !== operationId) { return; }
          if (error.response && error.response.status === 429) {
            self.retrievalBusy = false;
            self.retrievalError = '보관번호 확인 요청이 많습니다. 잠시 후 다시 시도해 주세요.';
            return;
          }
          if (isAmbiguousRequestError(error)) {
            self.recoverRetrievalStart(operationId);
            return;
          }
          self.retrievalBusy = false;
          self.retrievalError = '보관 정보를 확인하지 못했습니다. 보관번호를 다시 확인해 주세요.';
        });
      },
      acceptRetrievalResult: function (result, operationId) {
        if (this.operationId !== operationId) {
          KioskTransactionService.cancelRetrieval(result.transactionId).catch(function () {});
          return;
        }
        this.clearRecovery();
        this.retrievalBusy = false;
        this.transactionId = result.transactionId;
        this.activeTransactionKind = 'RETRIEVING';
        this.selectedLocker = { lockerId: result.lockerId, status: 'OCCUPIED' };
        this.additionalFee = Number(result.additionalFee || 0);
        this.go(KIOSK_STATES.RETRIEVE_CONFIRM);
      },
      recoverRetrievalStart: function (operationId) {
        var self = this;
        var retrievalCode = this.retrievalCodeInput;
        this.recoveryBusy = true;
        this.recoveryMessage = '거래 상태를 확인하고 있습니다...';
        this.recoveryAction = null;
        KioskTransactionService.recoverRetrieval(retrievalCode).then(function (result) {
          if (result.status === 'STORED') {
            return KioskTransactionService.startRetrieval(retrievalCode);
          }
          return result;
        }).then(function (result) {
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
        this.currentCaptureId = null;
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
        instructions[KIOSK_STATES.RETRIEVAL_CODE] = '보관번호를 입력해 주세요.';
        instructions[KIOSK_STATES.RETRIEVE_CONFIRM] = '찾을 보관함 정보를 확인해 주세요.';
        instructions[KIOSK_STATES.RETRIEVE_SETTLEMENT] = '추가 정산 금액을 확인해 주세요.';
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
        this.idImage = null;
        this.idError = '';
        this.capturedFaceBase64 = '';
        this.capturedFacePreview = '';
        this.currentCaptureId = null;
        this.verificationAttempts = 0;
        this.verificationPassed = false;
        this.verificationMessage = '';
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
        return { AVAILABLE: '사용 가능', RESERVED: '예약 중', OCCUPIED: '사용 중', DISABLED: '점검 중' }[status] || status;
      },
      lockerAriaLabel: function (locker) {
        return locker.lockerId + '번 보관함, ' + this.lockerStatusText(locker.status) +
          (this.selectedLocker && this.selectedLocker.lockerId === locker.lockerId ? ', 선택됨' : '');
      },
      twoDigits: function (number) { return String(number).padStart(2, '0'); }
    }
  });
}());
