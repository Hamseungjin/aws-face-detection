# AWS Face Detection 프로젝트 요약

## 1. 프로젝트 목적

실시간 카메라 프레임을 AWS에서 분석하고 결과를 저장·조회하는 서버리스 영상 분석 프로젝트.

```text
카메라
→ Kinesis
→ Lambda
→ Rekognition
→ S3 / DynamoDB
→ API Gateway
→ Web UI
```

필요하면 특정 객체 감지 시 SNS 알림도 전송한다.

---

## 2. 주요 AWS 구성

| 서비스            | 역할             |
| -------------- | -------------- |
| Kinesis        | 카메라 프레임 전송     |
| Lambda         | 프레임 처리 / 얼굴 비교 |
| Rekognition    | 객체·얼굴 분석       |
| S3             | 이미지 저장         |
| DynamoDB       | 분석 데이터 저장      |
| SNS            | 감지 알림          |
| API Gateway    | Web UI API     |
| CloudFormation | AWS 인프라 배포     |

---

## 3. 주요 설정

```text
config/global-params.json
→ CloudFormation Stack 설정

config/cfn-params.json
→ S3, Lambda, API Gateway 등 인프라 설정

config/imageprocessor-params.json
→ Rekognition / S3 / DynamoDB 설정

config/framefetcher-params.json
→ 최근 프레임 조회 설정
```

---

## 4. 기본 배포

```bash
export AWS_DEFAULT_REGION=ap-northeast-2
export AWS_REGION=ap-northeast-2
pynt packagelambda
pynt deploylambda
pynt createstack
pynt stackstatus
```

기존 방식의 Web UI:

```bash
pynt webui
pynt webuiserver
```

```text
http://localhost:8080
```

---

## 5. 카메라 실행

웹캠:

```bash
pynt videocapture[60,300]
```

* 60프레임마다 1장 전송
* 300초 후 자동 종료

IP 카메라:

```bash
pynt videocaptureip["http://<camera-ip>/video",60]
```

전송 빈도를 높일수록 Rekognition 호출과 비용도 증가한다.

---

## 6. 시민용 키오스크

```text
/kiosk
```

키오스크 흐름:

```text
STORE
신분증 Bytes + FRAME-A Bytes
→ FastAPI
→ Rekognition CompareFaces
→ 인증 성공 후 store complete 시
  S3 locker-references/<transaction_id>/reference.jpg 1장만 저장

RETRIEVE
S3 reference.jpg + FRAME-B Bytes
→ FastAPI
→ Rekognition CompareFaces
→ 성공 시 RETRIEVED 후 reference.jpg 삭제
```

키오스크 STORE/RETRIEVE는 Rekognition Collection + SQLite + OpenCV만 사용한다.
Kinesis / DynamoDB / Lambda / API Gateway / 운영자 capture-frame은 repository에서 제거했다.
활성 legacy `locker-references/` 거래가 있으면 S3 retrieve 경로만 유지한다.

결제 및 보관함 동작은 데모이며 얼굴 비교는 실제 AWS Rekognition을 사용한다.

---

## 7. 얼굴 비교

API:

```http
POST /face-compare
```

주요 기능:

```text
신분증 얼굴 DetectFaces
+
카메라 프레임 얼굴
↓
CompareFaces
↓
유사도 비교
```

기본 유사도 기준:

```text
90%
```

결과:

```text
matched
similarity
threshold
reason
targetFrameId
```

---

## 8. EC2 배포

Web UI와 KPI Dashboard를 별도의 EC2 스택으로 배포할 수 있다.

```bash
pynt ec2vpcinfo
pynt publishapps
pynt createec2stack
pynt ec2ip
```

구성:

```text
EC2 #1 → Web UI :8080
EC2 #2 → KPI Dashboard :8000
```

EventBridge Scheduler로 업무시간 자동 start/stop도 가능하다.

---

## 9. 주요 명령어

```bash
# Lambda 패키징
pynt packagelambda

# Lambda 업로드
pynt deploylambda

# AWS 스택 생성
pynt createstack

# 상태 확인
pynt stackstatus

# 카메라 실행
pynt videocapture[60,300]

# EC2 앱 배포
pynt publishapps

# EC2 생성
pynt createec2stack

# EC2 삭제
pynt deleteec2stack

# 데이터 스택 삭제
pynt deletestack
```

---

## 10. 주의사항

* Rekognition 호출량에 따라 비용 발생
* 테스트 후 사용하지 않는 AWS 리소스 삭제
* AWS 자격증명을 코드에 직접 저장하지 않기
* 신분증 이미지는 개인정보/생체정보이므로 취급 주의
* `deletestack` 실행 시 저장 데이터가 삭제될 수 있음

---

## 한 줄 요약

**카메라 프레임을 Kinesis → Lambda → Rekognition으로 분석하고 S3/DynamoDB에 저장한 뒤 Web UI에서 조회하며, 키오스크에서는 촬영한 얼굴과 신분증 얼굴을 비교하는 AWS 서버리스 프로젝트이다.**
