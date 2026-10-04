# 개발 및 검증

Python 3.14와 Linux 환경에서 `requirements_test.txt`를 설치합니다.

- 테스트: `python -m pytest --cov=custom_components.kepco_on --cov-fail-under=95`
- 정적 검사: `python -m ruff check .`, `python -m mypy`
- 포맷 검사: `python -m ruff format --check .`
- 개인정보가 없는 가상 응답만 테스트에 사용합니다.

GitHub Actions에서 Tests 및 HACS/Hassfest 검증을 통과한 커밋으로 릴리스를 생성합니다.
현재 릴리스 ZIP에는 한전ON 모듈만 포함됩니다. 새 모듈 추가 시 패키징 방식도 함께 확장합니다.
