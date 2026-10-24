# 배포 파일의 라이선스와 설치 검증

배포 메타데이터는 `license = "MPL-2.0"`, `license-files = ["LICENSE"]`를 사용한다. LICENSE 본문과 의도한 MPL-2.0 라이선스, runtime Python 최소 버전(`>=3.11`), runtime 의존성, 패키지 버전은 유지한다. `setuptools>=77.0.3`은 **빌드 도구의 최소 버전**이다. Python 지원 범위를 바꾼 것이 아니다.

이전 TOML 표 형식과 License classifier의 사용 중단 경고를 없애기 위한 변경이다. 근거는 [setuptools 이전 안내](https://setuptools.pypa.io/en/stable/userguide/license_migration.html)와 [Python Packaging Guide](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#license-and-license-files)다. Packaging Guide가 제시하는 최소값 77.0.3을 실제로 고정해서 검증한다.

## 검증 방식

[packaging-smoke CI](../.github/workflows/ci.yml)는 Python 3.11의 별도 빌드 venv에서 **정확히 setuptools 77.0.3**과 **현재 설치 가능한 setuptools**를 각각 시험한다. 각 빌드 직전에 실제 backend 버전을 출력한다. `--no-isolation`은 이미 격리해 고정한 venv의 backend를 사용하게 하는 옵션이다. frontend가 별도 환경에 최신 backend를 다시 설치해 최소 버전 시험을 무효로 만드는 것을 방지한다.

각 환경에서 다음 세 경로를 빌드한다.

1. 소스에서 sdist 생성
2. 소스에서 직접 wheel 생성
3. 생성한 sdist를 안전하게 풀어 같은 backend로 wheel 재생성

[scripts/check_distribution.py](../scripts/check_distribution.py)는 sdist와 두 wheel의 메타데이터, 원본 LICENSE 바이트, rules·웹 자산·평가/보정 자료의 내용을 검사한다. `License-Expression: MPL-2.0`, `License-File: LICENSE`를 요구하고, 이전 `License` 필드와 License classifier가 남아 있으면 실패한다. sdist/wheel의 runtime 요구 조건과 CLI 진입점도 확인한다. 경로 이탈·중복 archive member·링크를 거부한다. 빌드 로그에서는 위 **두 라이선스 경고만** 검출하며 다른 종류의 경고를 일괄 금지하지 않는다.

직접 wheel과 sdist에서 만든 wheel은 각각 새 설치 venv에서 검사한다. checkout 밖에서 `PYTHONPATH` 없이 실행해, import가 해당 venv 아래에서 오는지 확인하고 CLI `--help`, 웹 자산, 정답 평가 자료 5개와 fake benchmark를 실행한다. 실제 모델이나 인증 정보는 필요 없다. 패키지를 발행하거나 배포하지 않는다.

## 로컬 재현

깨끗한 checkout 또는 export에서 실행한다. 아래 폴더는 새 폴더여야 한다. Python 3.11을 선택하고, 현재 backend 시험은 **다른 깨끗한 소스·venv·출력 폴더**에서 `setuptools==77.0.3` 대신 `setuptools`를 설치해 반복한다. 정확한 전체 CI 명령과 로그 검사는 위 workflow에 있다.

```sh
python3.11 -m venv .build-env
.build-env/bin/pip install build==1.6.1 wheel==0.48.0 setuptools==77.0.3
.build-env/bin/python -c "import setuptools; print(setuptools.__version__)"
.build-env/bin/python -m build --no-isolation --sdist --outdir dist/sdist
.build-env/bin/python -m build --no-isolation --wheel --outdir dist/direct
.build-env/bin/python scripts/check_distribution.py --sdist dist/sdist/*.tar.gz --wheels dist/direct/*.whl
mkdir .from-sdist
.build-env/bin/python -c "import glob, tarfile; tarfile.open(glob.glob('dist/sdist/*.tar.gz')[0]).extractall('.from-sdist', filter='data')"
.build-env/bin/python -m build .from-sdist/verinote-0.1.0 --no-isolation --wheel --outdir "$PWD/dist/from-sdist"
.build-env/bin/python scripts/check_distribution.py --sdist dist/sdist/*.tar.gz --wheels dist/direct/*.whl dist/from-sdist/*.whl
```

현재 backend의 버전은 시간이 지나면 바뀐다. 최소 버전은 고정 검증하고 현재 버전은 매 실행의 로그로 확인한다. sdist와 wheel의 archive SHA256이 서로 같을 필요는 없으며, 명시한 메타데이터와 LICENSE·자산 바이트를 비교한다.

## 2026-10-24 확인 결과

변경 전 커밋 `f8abc007509ea2b54dc7e2dd93b25aeeb52fd572`의 깨끗한 export를 Python 3.11.15 / setuptools **84.2.1**로 빌드해 TOML 표 라이선스와 License classifier 경고를 모두 재현했다. 관측한 첫 경고에는 2027-02-18 이전 안내가 있었지만, 모든 backend에서 같은 날 빌드가 중단된다는 뜻으로 일반화하지 않는다.

변경 후에는 **77.0.3과 84.2.1 각각에서 세 빌드 경로가 통과**했고 두 경고가 사라졌다. 네 wheel 설치 환경(두 backend × 직접/재생성)에서 CLI·웹·평가 5개·fake benchmark가 통과했다. LICENSE의 SHA256은 변경 전후 모두 `3f3d9e0024b1921b067d6f7f88deb4a60cbe7a78e76c64e3f1d7fc3b779b9d04`다. 이 결과는 해당 버전들의 확인 기록이며 미래 backend 전체의 호환성을 보증하지 않는다.
