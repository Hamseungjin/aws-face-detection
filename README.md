## 1. 이 프로젝트의 목적

이 프로젝트는 **실시간 카메라 영상 프레임을 AWS에서 분석하고, 특정 객체가 감지되면 알림을 보내는 서버리스 영상 분석 파이프라인**입니다.

전체 흐름은 다음과 같습니다.

```text
카메라 영상 캡처
→ AWS Kinesis로 프레임 전송
→ Lambda가 Rekognition으로 이미지 분석
→ S3에 프레임 저장
→ DynamoDB에 분석 메타데이터 저장
→ SNS로 알림 발송
→ 웹 UI에서 결과 확인
```

## 2. 핵심 구성 요소

| 구성 요소                    | 역할                          |
| ------------------------ | --------------------------- |
| IP 카메라 / 웹캠              | 영상을 캡처하는 입력 장치              |
| OpenCV                   | 영상에서 프레임을 추출                |
| Kinesis                  | 캡처한 프레임을 AWS로 스트리밍          |
| Lambda - Image Processor | 프레임을 받아 Rekognition으로 분석    |
| Amazon Rekognition       | 이미지 안의 객체, 사람, 동물, 가방 등을 감지 |
| S3                       | 캡처된 프레임 이미지를 저장             |
| DynamoDB                 | 분석 결과와 메타데이터 저장             |
| SNS                      | 특정 객체 감지 시 SMS 또는 알림 발송     |
| Lambda - Frame Fetcher   | 웹 UI가 조회할 최신 프레임 데이터를 제공  |
| API Gateway              | 웹 UI와 Lambda를 연결하는 API       |
| 웹 UI                    | 분석된 프레임과 라벨을 브라우저에서 확인    |

## 3. 필요한 사전 준비

개발 환경에는 다음이 필요합니다.

* AWS 계정 및 관리자 권한 IAM 사용자
* Python, pip, virtualenv
* AWS CLI 설정
* OpenCV 3
* boto3
* pynt
* pytz
* IP 카메라, 스마트폰 IP 카메라 앱, 노트북 웹캠 또는 USB 웹캠

AWS 리전은 사용하는 서비스가 모두 지원되는 곳으로 선택해야 합니다. 원문에서는 `us-east-1`, `us-west-2`, `eu-west-1`을 예시로 들고 있으며, 사용자가 작성한 명령어에는 `ap-northeast-2`도 포함되어 있습니다.

## 4. 주요 설정 파일

### `config/global-params.json`

CloudFormation 스택 이름을 설정합니다.

```json
{
  "StackName": "video-analyzer-stack"
}
```

### `config/cfn-params.json`

CloudFormation 배포에 필요한 S3 버킷, Lambda ZIP 경로, API Gateway 이름 등을 설정합니다.

중요 항목은 다음과 같습니다.

* `SourceS3BucketParameter`: Lambda ZIP 파일을 업로드할 S3 버킷
* `FrameS3BucketNameParameter`: 캡처 프레임 이미지를 저장할 S3 버킷
* `ApiGatewayRestApiNameParameter`: API Gateway 이름
* `ApiGatewayStageNameParameter`: API 배포 스테이지 이름

### `config/imageprocessor-params.json`

Image Processor Lambda가 실행 중 사용할 설정입니다.

중요 항목은 다음과 같습니다.

* `s3_bucket`: 프레임 이미지를 저장할 S3 버킷
* `ddb_table`: 메타데이터 저장용 DynamoDB 테이블
* `rekog_max_labels`: Rekognition이 반환할 최대 라벨 수
* `rekog_min_conf`: 라벨 인식 최소 신뢰도
* `label_watch_list`: 알림을 보낼 감지 대상 라벨 목록
* `label_watch_min_conf`: 알림 발생 최소 신뢰도
* `label_watch_phone_num`: SMS 수신 전화번호
* `label_watch_sns_topic_arn`: SNS Topic ARN

### `config/framefetcher-params.json`

웹 UI가 최신 프레임을 조회할 때 사용하는 설정입니다.

중요 항목은 다음과 같습니다.

* `s3_pre_signed_url_expiry`: S3 이미지 접근 URL 만료 시간
* `ddb_table`: 조회할 DynamoDB 테이블
* `fetch_horizon_hrs`: 최근 몇 시간 이내의 프레임만 조회할지 설정
* `fetch_limit`: 한 번에 가져올 프레임 개수

## 5. 배포 및 실행 순서

실제로 사용할 때는 아래 순서만 기억하면 됩니다.

```bash
pynt packagelambda
```

Lambda 코드를 ZIP 파일로 패키징합니다.

```bash
pynt deploylambda
```

패키징된 Lambda ZIP 파일을 S3에 업로드합니다.

```bash
pynt createstack
```

CloudFormation으로 Kinesis, Lambda, S3, DynamoDB, API Gateway, IAM Role 등을 생성합니다.

```bash
pynt stackstatus
```

스택 생성 상태를 확인합니다.

```bash
pynt webui
```

웹 UI 설정 파일을 생성합니다. API Gateway URL과 API Key가 포함된 `apigw.js`가 만들어집니다.

```bash
pynt webuiserver
```

로컬에서 웹 UI 서버를 실행합니다.

브라우저에서 다음 주소로 접속합니다.

```text
http://localhost:8080
```

웹캠을 사용할 경우 다음 명령어를 실행합니다. 대괄호 안의 숫자는 `capture_rate`(N프레임마다 1장
전송)이며, **값이 클수록 전송 프레임 수와 Rekognition 호출이 줄어 비용이 절감**됩니다.
인자를 생략하면 기본값 `60`이 적용됩니다.

```bash
pynt videocapture          # 기본값 60 (개발/시연 권장)
pynt videocapture[60]      # 개발/시연 권장
pynt videocapture[90]      # 저비용 테스트 권장
pynt videocapture[20]      # 고속 테스트가 필요할 때만 (비용 증가)
```

비용 누적을 막기 위해 **실행 시간 자동 종료**를 함께 지정할 수 있습니다. 두 번째 인자는 초 단위이며,
지정한 시간이 지나면 클라이언트가 스스로 종료합니다.

```bash
pynt videocapture[60,300]  # 60프레임마다 1장 전송, 300초 후 자동 종료
```

> 캡처 시작 시 콘솔과 로그에 추정 fps·초/분/시간당 전송 프레임 수와 Rekognition 비용 경고가
> 출력됩니다. Rekognition `DetectLabels` 호출 자체를 끄려면 `config/imageprocessor-params.json`의
> `enable_detect_labels`를 `false`로 두거나(재배포 필요), Lambda 환경변수 `ENABLE_DETECT_LABELS=false`로
> 설정하세요. 이 경우에도 프레임은 S3·DynamoDB에 그대로 저장되고 얼굴 비교 기능은 정상 동작합니다.

IP 카메라나 스마트폰 MJPEG 스트림을 사용할 경우 다음 명령어를 실행합니다.

```bash
pynt videocaptureip["http://192.168.0.2/video",60]
```

## 6. 주요 빌드 명령어 정리

| 명령어                   | 기능                            |
| --------------------- | ----------------------------- |
| `pynt packagelambda`  | Lambda 함수 코드를 ZIP으로 패키징       |
| `pynt deploylambda`   | Lambda ZIP 파일을 S3에 업로드        |
| `pynt createstack`    | AWS 인프라 전체 생성                 |
| `pynt stackstatus`    | CloudFormation 스택 상태 확인       |
| `pynt webui`          | 웹 UI 빌드 및 API 설정 파일 생성        |
| `pynt webuiserver`    | 로컬 웹 UI 서버 실행                 |
| `pynt videocapture`   | 노트북 또는 USB 웹캠에서 프레임 캡처 (기본 rate 60, `[rate,초]`로 자동 종료) |
| `pynt videocaptureip` | IP 카메라 MJPEG 스트림에서 프레임 캡처     |
| `pynt deletedata`     | S3 프레임 이미지와 DynamoDB 메타데이터 삭제 |
| `pynt deletestack`    | 생성한 AWS 인프라 삭제                |
| `pynt ec2vpcinfo`     | 기본 VPC와 퍼블릭 서브넷 ID 조회 (EC2 배포용) |
| `pynt publishapps`    | webui/KPI 앱 artifact를 S3로 업로드 (`[webui]`/`[kpi]` 개별 가능) |
| `pynt createec2stack` | EC2 스택(webui + KPI + 스케줄러) 생성 |
| `pynt updateec2stack` | EC2 스택 업데이트 |
| `pynt ec2ip`          | EC2 인스턴스 현재 public IP 출력 |
| `pynt deleteec2stack` | EC2 스택만 삭제 (데이터 스택은 유지) |

## 7. 이 프로젝트로 만들 수 있는 기능

이 구조를 활용하면 다음 기능을 만들 수 있습니다.

### 실시간 보안 감시

카메라에 사람이 감지되면 SMS나 SNS 알림을 보낼 수 있습니다.

예시는 다음과 같습니다.

```text
사람 감지 → Rekognition 분석 → SNS 알림 발송 → 웹 UI에서 이미지 확인
```

### 출입 감지 시스템

문 앞, 사무실 입구, 창고 입구 등에 카메라를 설치하고 사람이 감지될 때만 이벤트를 기록할 수 있습니다.

### 영상 프레임 분석 대시보드

웹 UI를 통해 최근 캡처 프레임과 인식된 라벨을 확인할 수 있습니다.

### 서버리스 이미지 분석 파이프라인 학습

AWS 서버리스 구성 요소를 학습하는 데도 적합합니다.

특히 다음 기술을 실습할 수 있습니다.

* Kinesis 스트리밍
* Lambda 이벤트 처리
* Rekognition 이미지 분석
* S3 이미지 저장
* DynamoDB 메타데이터 저장
* SNS 알림
* API Gateway
* CloudFormation 인프라 자동화

## 8. 주의할 점

* 실험 후에는 반드시 `pynt deletestack`을 실행해 비용 발생을 방지해야 합니다.
* S3 버킷과 객체가 실제로 삭제되었는지 AWS 콘솔에서 확인하는 것이 좋습니다.
* `pynt webuiserver`는 터미널을 계속 점유하므로 실행 중인 터미널을 닫으면 웹 UI 서버도 종료됩니다.
* `label_watch_phone_num`을 설정하지 않으면 SMS 알림 기능은 활성화되지 않습니다.
* 이 스택은 개발 및 데모 목적에 가깝기 때문에 운영 환경에서는 보안, 인증, 권한, 비용 제어를 추가로 보완해야 합니다.

## 9. EC2 배포 (webui + KPI Dashboard)

기존 서버리스 파이프라인은 그대로 두고, **웹 UI와 KPI 대시보드를 EC2 2대**로 배포하고 평일 업무시간에만
자동 start/stop 하는 **별도 스택**(`video-analyzer-ec2-stack`)을 제공합니다.

* EC2 #1: 기존 웹 UI 서빙 (`:8080`), EC2 #2: 운영 KPI 대시보드 (`:8000`, FastAPI).
* EventBridge Scheduler로 **월~금 09:00/11:00/13:00/15:00 (Asia/Seoul)** 자동 start/stop.
* **Elastic IP로 접속 주소 고정** — stop/start 후에도 동일한 `http://<EIP>:포트`로 접속.
* 관리 접속은 **SSM Session Manager**(SSH/키페어 없음), 도메인/HTTPS 없음(데모용).

빠른 시작:

```text
pynt ec2vpcinfo          # VpcId/SubnetId 확인 후 config/ec2-params.json 채우기
pynt publishapps         # 앱 artifact를 S3 업로드
pynt createec2stack      # EC2 + 스케줄러 생성
pynt ec2ip               # public IP 확인 → 브라우저 접속
```

자세한 사전 조건 · 배포 · 검증 · 스케줄 동작 · 비용/보안 주의 · 삭제 · 트러블슈팅은
**[docs/EC2_DEPLOYMENT.md](docs/EC2_DEPLOYMENT.md)** 참고.

> 주의: EC2 스택은 비용이 발생합니다(EC2 컴퓨트는 running 시간, **EBS와 Elastic IP는 stopped 상태에서도 과금**).
> 고정 IP(EIP 2개)는 접속 편의성을 위한 선택으로 **월 약 +$7**가 추가됩니다.
> 검증 후 필요 시 수동 stop, 사용 종료 시 `pynt deleteec2stack`(EIP도 함께 release).

## 한 줄 요약

이 마크다운은 **카메라 영상을 AWS Kinesis, Lambda, Rekognition, S3, DynamoDB, SNS, API Gateway로 연결해 실시간 객체 감지, 알림, 웹 UI 모니터링을 구현하는 서버리스 영상 분석 프로젝트 가이드**입니다.

## 웹 UI 얼굴 검증

이 프로젝트는 기존의 `카메라 → Kinesis → Image Processor → S3/DynamoDB → 웹 UI` 흐름을 변경하지 않고, 업로드한 신분증 얼굴 이미지와 가장 최근 카메라 프레임의 얼굴을 비교할 수 있습니다.

### 기능 동작 방식

1. 웹 UI에서 운영자가 최대 5MiB 크기의 JPEG 또는 PNG 신분증 이미지를 선택합니다.
2. 브라우저는 로컬 미리보기를 표시하고, 이미지 바이트를 base64로 인코딩해 `POST /face-verify`로 전송합니다.
3. Face Verifier Lambda는 업로드된 신분증 이미지에 대해 Amazon Rekognition `DetectFaces`를 호출하여 얼굴 존재 여부를 확인하고, 선택된 얼굴의 경계 상자, 신뢰도, 품질, 자세 정보를 반환합니다.
4. Lambda는 `processed_year_month-processed_timestamp-index` GSI를 통해 `EnrichedFrame` DynamoDB 테이블에서 현재 월 또는 이전 월의 최신 프레임을 조회합니다.
5. 최신 프레임이 설정된 최신성 허용 시간 안에 있으면 Lambda는 업로드 이미지를 `SourceImage`, 최신 S3 프레임을 `TargetImage`로 사용해 Rekognition `CompareFaces`를 호출합니다.
6. UI는 매칭 여부, 유사도, 임계값, 사유, 최신 프레임 메타데이터, 신분증 이미지 얼굴 분석 결과를 표시합니다. 감지된 얼굴 경계 상자는 신분증 이미지 미리보기 위에 표시됩니다.


### Face Verifier 설정

예제 파일을 복사해 배포 시 사용할 설정 파일을 생성합니다.

```bash
cp config/faceverifier-params.example.json config/faceverifier-params.json
```

설정 예시는 다음과 같습니다.

```json
{
  "region": "ap-northeast-2",
  "ddb_table": "EnrichedFrame",
  "ddb_gsi_name": "processed_year_month-processed_timestamp-index",
  "timezone": "Asia/Seoul",
  "similarity_threshold": 90.0,
  "rekognition_api_similarity_threshold": 0.0,
  "quality_filter": "NONE",
  "latest_frame_horizon_minutes": 5,
  "max_source_image_bytes": 5242880,
  "allowed_source_content_types": ["image/jpeg", "image/png"],
  "allow_multiple_faces_in_id_image": false
}
```

`config/faceverifier-params.json`에는 환경별 배포 값이 들어가며, Git에서 의도적으로 제외됩니다.

### Face Verifier Lambda 배포

`config/cfn-params.json`에 `FaceVerifierSourceS3KeyParameter`를 추가한 뒤, Lambda 아티팩트를 패키징하고 업로드합니다.

```bash
python build.py packagelambda
python build.py deploylambda
python build.py updatestack
```

`packagelambda`는 `config/faceverifier-params.json`을 `build/faceverifier.zip`에 포함합니다. 예제 설정 파일은 문서화를 위한 용도이며, 런타임 설정으로 배포하지 않아야 합니다.

### 필요한 IAM 권한

CloudFormation 템플릿은 Face Verifier Lambda에 다음 권한을 부여합니다.

* `EnrichedFrame` 테이블과 `processed_year_month-processed_timestamp-index` GSI에 대한 `dynamodb:Query`
* 캡처 프레임 버킷 객체에 대한 `s3:GetObject`
* `rekognition:DetectFaces` 및 `rekognition:CompareFaces`
* Lambda 로그 기록을 위한 CloudWatch Logs 권한

### API 엔드포인트

배포된 API에는 다음 엔드포인트가 추가됩니다.

```http
POST /face-verify
```

요청 예시는 다음과 같습니다.

```json
{
  "id_image_base64": "<base64 encoded image bytes>",
  "id_image_content_type": "image/jpeg",
  "threshold": 90.0
}
```

`threshold`는 선택값입니다. 값을 생략하면 Lambda는 `faceverifier-params.json`의 `similarity_threshold` 값을 사용합니다.

성공 응답 예시는 다음과 같습니다.

```json
{
  "success": true,
  "matched": true,
  "similarity": 96.42,
  "threshold": 90.0,
  "reason": "SIMILARITY_ABOVE_THRESHOLD",
  "id_image_analysis": {
    "detected": true,
    "face_count": 1,
    "selected_face": {
      "bounding_box": {"left": 0.31, "top": 0.18, "width": 0.24, "height": 0.32},
      "confidence": 99.8,
      "quality": {"brightness": 82.1, "sharpness": 74.5},
      "pose": {"roll": 1.2, "yaw": -3.4, "pitch": 2.1}
    }
  },
  "latest_frame": {
    "frame_id": "f76af0fa-0c32-45a1-bbf5-bb06ca474d2a",
    "s3_bucket": "video-analyzer-frames",
    "s3_key": "frames/2026/06/15/10/f76af0fa.jpg",
    "processed_timestamp": 1781517600.123,
    "approx_capture_timestamp": 1781517599.812,
    "age_seconds": 2.14
  },
  "error": null
}
```

실패 응답 예시는 다음과 같습니다.

```json
{
  "success": false,
  "matched": false,
  "similarity": null,
  "threshold": 90.0,
  "reason": "NO_FACE_IN_ID_IMAGE",
  "id_image_analysis": {"detected": false, "face_count": 0, "selected_face": null},
  "latest_frame": null,
  "error": {
    "service": "rekognition",
    "code": null,
    "message": "No face was detected in the uploaded ID image."
  }
}
```

### 웹 UI 엔드포인트 설정

기존 `python build.py webui` 작업은 빌드 결과물의 `web-ui/src/apigw.js`에 `apiBaseUrl`과 `apiKey`를 기록합니다. 얼굴 검증 UI는 동일한 Axios 인스턴스를 사용하며, 해당 기본 URL을 기준으로 `face-verify`를 호출합니다. 따라서 CloudFormation 업데이트 후에는 별도의 엔드포인트 설정이 필요하지 않습니다.

### 로컬 및 배포 환경 테스트

Mock AWS 클라이언트를 사용해 단위 테스트를 실행합니다.

```bash
python3 -m pytest -q tests/test_faceverifier.py
```

배포 후에는 다음 순서로 확인합니다.

1. 최근 프레임이 S3와 DynamoDB에 기록되도록 카메라 캡처 클라이언트를 시작합니다.
2. 웹 UI를 빌드하고 실행합니다.
3. 웹 UI를 열고 JPEG 또는 PNG 신분증 이미지를 선택한 뒤, 필요하면 임계값을 조정하고 **얼굴 검증 실행**을 클릭합니다.
4. 신분증 이미지의 얼굴 경계 상자, 유사도 결과, 사유, 최신 프레임 메타데이터가 표시되는지 확인합니다.

### 개인정보 및 생체 데이터 관련 주의사항

* 적법한 권한과 규정을 준수하는 보관 및 처리 정책이 없는 경우 신분증 이미지를 업로드하지 않아야 합니다.
* UI 미리보기는 로컬에서 표시되지만, base64 이미지 바이트는 Rekognition 분석을 위해 API Gateway와 Lambda로 전송됩니다.
* Lambda는 원본 이미지 바이트, base64 페이로드, 전체 Rekognition 응답을 로그에 남기지 않도록 설계되어 있습니다.
* AWS 자격 증명은 런타임 환경 또는 IAM Role을 통해 제공되어야 하며, 코드나 설정 파일에 직접 넣으면 안 됩니다.
