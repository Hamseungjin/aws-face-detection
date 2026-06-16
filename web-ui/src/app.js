// Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Licensed under the Amazon Software License (the "License"). You may not use this file except in compliance with the License. A copy of the License is located at
//     http://aws.amazon.com/asl/
// or in the "license" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and limitations under the License.

if(!apiBaseUrl || !apiKey){
    alert("API base URL and/or API key are not set.")
}

var axiosInstance = axios.create({
  baseURL: apiBaseUrl, //From apigw.js
  headers: {'X-api-key': apiKey}, //From apigw.js
  timeout: 6000,
});

// Separate instance for face comparison: Rekognition CompareFaces can take
// noticeably longer than the frame list fetch, so use a larger timeout.
var faceCompareAxios = axios.create({
  baseURL: apiBaseUrl,
  headers: {'X-api-key': apiKey},
  timeout: 30000,
});


var app = new Vue({
  el: '#app',
  
  methods: {
  	fetchFrames: function(){
  		axiosInstance.get('enrichedframe')
			.then(response => {
		      // JSON responses are automatically parsed.
		      console.log(response.data);
		      this.enrichedframes = response.data;
		    })
		    .catch(e => {
		      //this.errors.push(e);
		      console.log(e);
		    })
  	},
  	toggleFetchFrames: function(){
  		if(!this.autoload){
  			//this.autoloadTimer.stop();
  			this.autoloadTimer = setInterval(this.fetchFrames, 3000);
  			this.autoload=true;
  		}
  		else{
  			//this.autoloadTimer.start();
  			clearInterval(this.autoloadTimer);
  			this.autoload = false;
  		}

  	},
  	handleFileSelect: function(event){
  		this.faceCompareError = null;
  		this.faceCompareResult = null;
  		this.uploadedImageBase64 = '';
  		this.uploadedImageDataUrl = '';
  		var file = event.target.files && event.target.files[0];
  		if(!file){ return; }
  		// Client-side pre-checks: only JPG/PNG, and keep payload under the
  		// Lambda 6MB sync limit (base64 inflates ~33%, so cap raw at 4MB).
  		if(['image/jpeg','image/png'].indexOf(file.type) === -1){
  			this.faceCompareError = "JPG/PNG 이미지만 업로드할 수 있어요.";
  			event.target.value = '';
  			return;
  		}
  		if(file.size > 4 * 1024 * 1024){
  			this.faceCompareError = "이미지 크기는 4MB 이하여야 해요.";
  			event.target.value = '';
  			return;
  		}
  		this.uploadedFilename = file.name;
  		this.uploadedContentType = file.type;
  		var reader = new FileReader();
  		reader.onload = (e) => {
  			this.uploadedImageDataUrl = e.target.result;             // data URL for preview
  			this.uploadedImageBase64 = e.target.result.split(',')[1]; // base64 payload only
  		};
  		reader.onerror = () => { this.faceCompareError = "파일을 읽지 못했어요."; };
  		reader.readAsDataURL(file);
  	},
  	compareFace: function(){
  		if(!this.uploadedImageBase64){ return; }
  		this.isComparingFace = true;
  		this.faceCompareError = null;
  		this.faceCompareResult = null;
  		faceCompareAxios.post('face-compare', {
  			imageBase64: this.uploadedImageBase64,
  			filename: this.uploadedFilename,
  			contentType: this.uploadedContentType,
  			similarityThreshold: this.similarityThreshold
  		})
  		.then(response => {
  			this.faceCompareResult = response.data;
  		})
  		.catch(e => {
  			if(e.response && e.response.data){
  				// Lambda returned a structured result with a 4xx/5xx status.
  				this.faceCompareResult = e.response.data;
  			} else {
  				this.faceCompareError = e.message || "요청 실패";
  			}
  		})
  		.then(() => {
  			this.isComparingFace = false;
  		})
  	},
  	frameTime: function(target){
  		if(!target || target.processed_timestamp == null){ return ''; }
  		return new Date(target.processed_timestamp * 1000).toString();
  	},
  	reasonText: function(reason){
  		var map = {
  			"NO_RECENT_FRAME": "최근 프레임이 너무 오래됐어요 (캡처가 실행 중인지 확인).",
  			"NO_LATEST_FRAME": "저장된 프레임이 없어요. 먼저 videocapture로 프레임을 만들어주세요.",
  			"NO_FACE_IN_SOURCE_OR_TARGET": "업로드 이미지 또는 프레임에서 얼굴을 찾지 못했어요.",
  			"UNSUPPORTED_IMAGE_FORMAT": "지원하지 않는 형식이에요 (JPG/PNG).",
  			"IMAGE_TOO_LARGE": "이미지가 너무 커요.",
  			"INVALID_IMAGE": "이미지를 해석하지 못했어요.",
  			"FRAME_METADATA_INVALID": "프레임 메타데이터가 올바르지 않아요.",
  			"ACCESS_DENIED": "권한 오류예요.",
  			"THROTTLED": "요청이 많아 잠시 후 다시 시도해주세요.",
  			"AWS_API_ERROR": "AWS 처리 중 오류가 났어요.",
  			"INVALID_S3_OBJECT": "프레임 이미지를 읽지 못했어요.",
  			"BAD_REQUEST": "요청 형식이 잘못됐어요."
  		};
  		return map[reason] || reason;
  	}
  },
  created: function () {
    this.toggleFetchFrames();
  },
  data: {
    enrichedframes : [],
    autoload: false,
  	autoloadTimer : null,
    uploadedImageBase64: '',
    uploadedImageDataUrl: '',
    uploadedFilename: '',
    uploadedContentType: '',
    similarityThreshold: 80,
    isComparingFace: false,
    faceCompareResult: null,
    faceCompareError: null,
  },
})



