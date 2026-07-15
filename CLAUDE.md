# Codex 오케스트레이션 워크플로

이 프로젝트는 아래 3역할 분업으로 진행한다. 다른 프로젝트에 적용하려면 이 파일만 복사하면 된다.

## 역할

| 역할 | 담당 | 모델/도구 |
|---|---|---|
| 계획 | 사용자 × ChatGPT Sol | 주문서 작성 — **주문서가 정본** |
| 구현·수정·잡문서 | Codex Luna | `codex exec --sandbox workspace-write -m gpt-5.6-luna -c model_reasoning_effort="high"` |
| 최종 QA (1회) | Codex Sol | `codex exec --sandbox read-only -m gpt-5.6-sol` (medium 기본) |
| 검수·판단·릴리스 | Claude (총괄 실행자) | 아래 절차 수행 |

## 절차

1. 주문서 전제와 저장소 상태만 짧게 확인한다 (HEAD, worktree clean, 기준 테스트).
2. Luna에게 주문서를 넘겨 구현시킨다. **구현과 함께 로컬 커밋 분리, read-only 진단 스크립트 실행, 최종 보고 초안까지 Luna 산출물로 요구한다** (codex 샌드박스는 네트워크만 차단, `git commit` 가능).
3. Claude가 커밋별 diff를 직접 검수하고 결함 목록을 확정한다. **수정은 직접 하지 않고 Luna 세션을 resume해서 시킨다.** Claude는 수정 diff 재검수만 한다. **같은 결함에 대해 Luna 수정이 1회 실패하면 Claude가 직접 고친다** (모델 한계 인정, 무한 반복 금지).
4. Sol Medium 읽기 전용 QA를 **한 번만** 호출한다.
5. QA 지적은 Claude가 검증해 실제 결함만 확정하고, 수정은 다시 Luna resume으로.
6. 전체 테스트 통과 후 push, 커밋별 CI green + JUnit XML 직접 파싱 확인.
7. 보고: 변경 파일, 검증 명령·결과, 커밋 SHA, push 결과, 남은 위험만.

## 위임 금지 (Claude가 직접)

- **diff 검수와 최종 판단.** Luna의 "검증 완료" 보고는 신뢰하지 않는다 — 실제로 결함이 남은 채 완료 보고한 전례가 있다.
- **전체 테스트 실행.** Luna 샌드박스는 temp 권한 문제로 전체 pytest가 깨질 수 있으므로 Claude 환경에서 직접 실행한다.
- push, CI 확인, 이슈 게시.

## 금지

- 주문서 재설계, 임의 범위 확대. 치명적 전제 오류나 새 설계 결정이 필요할 때만 중단하고 사용자에게 보고.
- 세 번째 모델 추가 배치 — 잡일은 도구 호출이라 토큰을 거의 안 먹고, 위임하면 검수 오버헤드만 는다. 예외: 대량 로그 요약 같은 자기완결적 소화 작업.
- Sol QA 반복 호출.

## codex-cli 호출 참고 (0.144.4 기준 검증)

- 비대화형: `codex exec --sandbox <read-only|workspace-write> -m <model> [-c model_reasoning_effort="high"] "프롬프트"` (stdin `-` 가능)
- 스레드 유지: 출력 헤더의 `session id:`를 캡처해 `codex exec -m <model> resume <SESSION_ID> "후속"`. 플래그는 `resume` 앞. `resume --last`는 병행 스레드 시 위험.
- resume 시 `-m` 생략하면 기본 모델로 떨어지므로 매 호출 명시.
- 인증: ChatGPT 로그인 (`codex login status`).
- PowerShell은 긴 한글 프롬프트 인코딩이 깨질 수 있으니 codex 호출·출력 리다이렉트는 Bash 사용.
