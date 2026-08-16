# AWS 비용 분석 요약

## 1. 핵심 결론

**EC2를 중지해도 AWS 비용은 0원이 되지 않는다.**

중지 시 없어지는 비용:

```text
EC2 인스턴스 컴퓨팅 비용
```

계속 발생하는 비용:

```text
Elastic IP 3개
Kinesis 1 Shard
EBS 36GB
```

예상 유지 비용:

```text
약 $25 / 월
약 3.5만 원 / 월
```

---

## 2. 현재 주요 리소스

```text
EC2
- t3.micro × 2
- t3.small × 1

Elastic IP
- 3개

EBS
- 8GB + 8GB + 20GB
- 총 36GB

Kinesis
- FrameStream
- 1 Shard
```

특히 `t3.small` 인스턴스 1개는 **CloudFormation 스택 밖에서 별도로 존재**한다.

---

## 3. 중지해도 발생하는 비용

| 리소스             |   예상 월 비용 |
| --------------- | --------: |
| Kinesis 1 Shard |     약 $11 |
| Elastic IP 3개   |     약 $11 |
| EBS 36GB        |    약 $3.3 |
| 기타              |     거의 $0 |
| **총합**          | **약 $25** |

Lambda, DynamoDB, API Gateway 등은 요청이 없으면 비용이 거의 발생하지 않는다.

---

## 4. 자동 재시작 주의

EventBridge Scheduler가 활성화되어 있어 EC2를 직접 중지해도 다시 켜질 수 있다.

```text
평일 09:00 → Start
평일 11:00 → Stop

평일 13:00 → Start
평일 15:00 → Stop
```

따라서 **EC2를 계속 꺼두려면 Scheduler도 확인해야 한다.**

---

## 5. 비용 확인 우선순위

1. 스택 밖 `t3.small` 인스턴스 확인
2. EventBridge 자동 시작 Scheduler 확인
3. Kinesis `FrameStream` 필요 여부 확인
4. Elastic IP 3개 필요 여부 확인
5. EBS 36GB 필요 여부 확인

---

## 6. 주요 확인 명령어

EC2:

```bash
aws ec2 describe-instances
```

EBS:

```bash
aws ec2 describe-volumes
```

Elastic IP:

```bash
aws ec2 describe-addresses
```

Kinesis:

```bash
aws kinesis describe-stream-summary \
  --stream-name FrameStream
```

Scheduler:

```bash
aws scheduler list-schedules
```

---

## 7. 완전히 정리하려면

EC2 스택 삭제:

```bash
pynt deleteec2stack
```

서버리스 데이터 스택 삭제:

```bash
pynt deletestack
```

단, **스택 밖에 있는 t3.small 인스턴스 + EIP + EBS는 별도로 정리해야 한다.**

---

## 한 줄 요약

**EC2를 Stop하면 컴퓨팅 비용만 멈추며, Kinesis·Elastic IP·EBS 때문에 약 월 3.5만 원의 비용은 계속 발생한다.**
