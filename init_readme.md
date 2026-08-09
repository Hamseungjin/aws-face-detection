
현재 화면 구조 : 

web-ui/index.html
        ↓
web-ui/src/app.js
        ↓
Vue 2
        ↓
Axios
        ↓
FastAPI


현재 화면의 목적 : 

로그인
↓
카메라 촬영
↓
AWS로 프레임 보내기
↓
최근 프레임 목록 보기
↓
Rekognition DetectLabels ON/OFF
↓
신분증 이미지 업로드
↓
얼굴 비교 결과 보기

만들고자하는 화면 목표:

환영                 UI
↓
보관 / 찾기          UI
↓
개인정보 동의         UI
↓
신분증 인증           Demo 입력
↓
얼굴 촬영             실제 카메라 + AWS
↓
본인 인증             실제 /face-compare
↓
사물함 선택           MOCK
↓
결제                  MOCK
↓
문 열림               MOCK
↓
완료                  UI


------------

현재 /face-compare는:

“내가 방금 촬영한 프레임”

을 비교하는 게 아닙니다.

대신 DynamoDB에 있는 가장 최신 프레임

을 가져옵니다.

사용자 A가 키오스크 앞에 있다고 하겠습니다.

A 얼굴 촬영
↓
Kinesis
↓
Lambda 처리 중...


그 사이 다른 곳에서 사용자 B 프레임이 들어온다면:

DynamoDB

B 얼굴 ← 최신
A 얼굴

그리고 A가 /face-compare를 호출하면

A 신분증
VS
B 얼굴

을 비교할 수도 있습니다.

아직 새 얼굴(B)이 DynamoDB에 안 들어가 있어서 이전 사람 얼굴을 가져올 수도 있습니다.
-------------
현재 capture-frame
↓
랜덤하게 새 frame_id 생성
↓
global latest frame

앞으로는

키오스크
↓
capture-frame

frameId = "abc123"
↓
Kinesis
↓
Lambda
↓
DynamoDB

frame_id = "abc123"

--------------

현재 얼굴 인증은 이런 질문에만 답합니다.

신분증 사진 속 사람
        VS
지금 카메라 앞 사람

→ 같은 사람인가?

AWS가 결과를 이렇게 준다고 생각하면 됩니다.

"네, 두 얼굴이 같은 사람일 확률이 높습니다."
similarity = 96%

그런데 AWS가 이런 식으로 말해주지는 않습니다.

"이 사람은 홍길동이고,
지난번 04번 사물함을 사용한 사람입니다."

이 차이입니다.

가장 단순한 방법은 거래번호를 발급하는 것