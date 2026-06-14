 
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
→ Web UI에서 결과 확인
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
| Lambda - Frame Fetcher   | Web UI가 조회할 최신 프레임 데이터를 제공  |
| API Gateway              | Web UI와 Lambda를 연결하는 API    |
| Web UI                   | 분석된 프레임과 라벨을 브라우저에서 확인      |

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

AWS 리전은 사용 서비스가 모두 지원되는 곳을 선택해야 합니다. 원문에서는 `us-east-1`, `us-west-2`, `eu-west-1`을 예시로 들고 있으며, 사용자가 작성한 명령어에는 `ap-northeast-2`도 포함되어 있습니다.

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

중요 항목:

* `SourceS3BucketParameter`: Lambda ZIP 파일을 올릴 S3 버킷
* `FrameS3BucketNameParameter`: 캡처 프레임 이미지를 저장할 S3 버킷
* `ApiGatewayRestApiNameParameter`: API Gateway 이름
* `ApiGatewayStageNameParameter`: API 배포 스테이지 이름

### `config/imageprocessor-params.json`

Image Processor Lambda가 실행 중 사용할 설정입니다.

중요 항목:

* `s3_bucket`: 프레임 이미지를 저장할 S3 버킷
* `ddb_table`: 메타데이터 저장용 DynamoDB 테이블
* `rekog_max_labels`: Rekognition이 반환할 최대 라벨 수
* `rekog_min_conf`: 라벨 인식 최소 신뢰도
* `label_watch_list`: 알림을 보낼 감지 대상 라벨 목록
* `label_watch_min_conf`: 알림 발생 최소 신뢰도
* `label_watch_phone_num`: SMS 수신 전화번호
* `label_watch_sns_topic_arn`: SNS Topic ARN

### `config/framefetcher-params.json`

Web UI가 최신 프레임을 조회할 때 사용하는 설정입니다.

중요 항목:

* `s3_pre_signed_url_expiry`: S3 이미지 접근 URL 만료 시간
* `ddb_table`: 조회할 DynamoDB 테이블
* `fetch_horizon_hrs`: 최근 몇 시간 내 프레임만 조회할지
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

Web UI 설정 파일을 생성합니다. API Gateway URL과 API Key가 포함된 `apigw.js`가 만들어집니다.

```bash
pynt webuiserver
```

로컬에서 Web UI 서버를 실행합니다.

브라우저에서 접속:

```text
http://localhost:8080
```

웹캠을 사용할 경우:

```bash
pynt videocapture[20]
```

IP 카메라나 스마트폰 MJPEG 스트림을 사용할 경우:

```bash
pynt videocaptureip["http://192.168.0.2/video",20]
```

## 6. 주요 build command 정리

| 명령어                   | 기능                            |
| --------------------- | ----------------------------- |
| `pynt packagelambda`  | Lambda 함수 코드를 ZIP으로 패키징       |
| `pynt deploylambda`   | Lambda ZIP 파일을 S3에 업로드        |
| `pynt createstack`    | AWS 인프라 전체 생성                 |
| `pynt stackstatus`    | CloudFormation 스택 상태 확인       |
| `pynt webui`          | Web UI 빌드 및 API 설정 파일 생성      |
| `pynt webuiserver`    | 로컬 Web UI 서버 실행               |
| `pynt videocapture`   | 노트북/USB 웹캠에서 프레임 캡처           |
| `pynt videocaptureip` | IP 카메라 MJPEG 스트림에서 프레임 캡처     |
| `pynt deletedata`     | S3 프레임 이미지와 DynamoDB 메타데이터 삭제 |
| `pynt deletestack`    | 생성한 AWS 인프라 삭제                |

## 7. 이 프로젝트로 만들 수 있는 기능

이 구조를 활용하면 다음 기능을 만들 수 있습니다.

### 실시간 보안 감시

카메라에 사람이 감지되면 SMS나 SNS 알림을 보낼 수 있습니다.

예시:

```text
사람 감지 → Rekognition 분석 → SNS 알림 발송 → Web UI에서 이미지 확인
```

### 반려동물 감지

`label_watch_list`에 `Pet`, `Dog`, `Cat` 등을 넣으면 반려동물 감지 시스템으로 사용할 수 있습니다.

### 출입 감지 시스템

문 앞, 사무실 입구, 창고 입구 등에 카메라를 설치하고 사람이 감지될 때만 이벤트를 기록할 수 있습니다.

### 객체 감지 알림

가방, 장난감, 특정 물체 등 Rekognition이 인식 가능한 라벨을 기반으로 감지 알림을 만들 수 있습니다.

### 영상 프레임 분석 대시보드

Web UI를 통해 최근 캡처 프레임과 인식된 라벨을 확인할 수 있습니다.

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
* `pynt webuiserver`는 터미널을 계속 점유하므로 실행 중인 터미널을 닫으면 Web UI 서버도 종료됩니다.
* `label_watch_phone_num`을 설정하지 않으면 SMS 알림 기능은 활성화되지 않습니다.
* 이 스택은 개발/데모 목적에 가깝기 때문에 운영 환경에서는 보안, 인증, 권한, 비용 제어를 추가로 보완해야 합니다.

## 한 줄 요약

이 마크다운은 **카메라 영상을 AWS Kinesis, Lambda, Rekognition, S3, DynamoDB, SNS, API Gateway로 연결해 실시간 객체 감지와 알림, Web UI 모니터링을 구현하는 서버리스 영상 분석 프로젝트 가이드**입니다.
