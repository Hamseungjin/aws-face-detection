// Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Licensed under the Amazon Software License (the "License").

if(!apiBaseUrl || !apiKey){
    alert("API base URL and/or API key are not set.")
}

var axiosInstance = axios.create({
  baseURL: apiBaseUrl, //From apigw.js
  headers: {'X-api-key': apiKey}, //From apigw.js
  timeout: 20000,
});

var MAX_ID_IMAGE_BYTES = 5 * 1024 * 1024;
var ALLOWED_ID_IMAGE_TYPES = ['image/jpeg', 'image/png'];

var app = new Vue({
  el: '#app',
  computed: {
    selectedFaceBoxStyle: function(){
      var result = this.faceVerify.result;
      if(!result || !result.id_image_analysis || !result.id_image_analysis.selected_face){
        return null;
      }
      var box = result.id_image_analysis.selected_face.bounding_box;
      if(!box){ return null; }
      return {
        left: (box.left * 100) + '%',
        top: (box.top * 100) + '%',
        width: (box.width * 100) + '%',
        height: (box.height * 100) + '%'
      };
    }
  },
  methods: {
    fetchFrames: function(){
      axiosInstance.get('enrichedframe')
        .then(response => {
          console.log(response.data);
          this.enrichedframes = response.data;
        })
        .catch(e => {
          console.log(e);
        })
    },
    toggleFetchFrames: function(){
      if(!this.autoload){
        this.autoloadTimer = setInterval(this.fetchFrames, 3000);
        this.autoload=true;
      }
      else{
        clearInterval(this.autoloadTimer);
        this.autoload = false;
      }
    },
    onIdImageSelected: function(event){
      var file = event.target.files[0];
      this.faceVerify.result = null;
      this.faceVerify.errorMessage = null;
      this.faceVerify.file = null;
      if(this.faceVerify.previewUrl){
        URL.revokeObjectURL(this.faceVerify.previewUrl);
        this.faceVerify.previewUrl = null;
      }
      if(!file){ return; }
      if(ALLOWED_ID_IMAGE_TYPES.indexOf(file.type) === -1){
        this.faceVerify.errorMessage = 'Only JPEG and PNG ID images are supported.';
        return;
      }
      if(file.size > MAX_ID_IMAGE_BYTES){
        this.faceVerify.errorMessage = 'ID image must be 5 MiB or smaller.';
        return;
      }
      this.faceVerify.file = file;
      this.faceVerify.previewUrl = URL.createObjectURL(file);
    },
    fileToBase64: function(file){
      return new Promise(function(resolve, reject){
        var reader = new FileReader();
        reader.onload = function(){
          var value = reader.result || '';
          resolve(value.split(',')[1]);
        };
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    },
    runFaceVerify: function(){
      if(!this.faceVerify.file){ return; }
      this.faceVerify.loading = true;
      this.faceVerify.errorMessage = null;
      this.faceVerify.result = null;
      this.fileToBase64(this.faceVerify.file)
        .then(base64 => axiosInstance.post('face-verify', {
          id_image_base64: base64,
          id_image_content_type: this.faceVerify.file.type,
          threshold: this.faceVerify.threshold === '' ? null : Number(this.faceVerify.threshold)
        }))
        .then(response => {
          this.faceVerify.result = response.data;
          if(response.data && response.data.success === false && response.data.error){
            this.faceVerify.errorMessage = response.data.reason + ': ' + response.data.error.message;
          }
        })
        .catch(e => {
          console.log(e);
          this.faceVerify.errorMessage = 'Face verification request failed.';
        })
        .then(() => {
          this.faceVerify.loading = false;
        });
    },
    formatNumber: function(value){
      if(value === null || value === undefined){ return 'N/A'; }
      return (Math.round((Number(value) + 0.00001) * 100) / 100).toString();
    }
  },
  created: function () {
    this.toggleFetchFrames();
  },
  data: {
    enrichedframes : [],
    autoload: false,
    autoloadTimer : null,
    faceVerify: {
      file: null,
      previewUrl: null,
      threshold: 90.0,
      loading: false,
      result: null,
      errorMessage: null
    }
  },
})
