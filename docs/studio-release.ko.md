# Memdex Studio Windows 프리뷰 릴리스

Studio는 Memdex 저장소에서 개발하며 데스크톱 버전, Git 태그, Windows ZIP을 독립적으로 관리합니다. 첫 프리뷰는 `0.1.0-preview.1`입니다. 이미 만든 ZIP으로 시작하려면 압축을 모두 풀고 안의 `Memdex-Studio.exe`를 실행하세요. Windows x64용이며 Electron, 터미널과 Python 런타임이 들어 있습니다. 실제 검토 요청에는 이 PC에 설정된 Claude Code, Ollama의 Qwen, DeepSeek Harness를 사용합니다.

## 버전과 빌드

1. `studio/package.json`의 버전과 `studio/package-lock.json`의 루트 패키지 버전을 일치시킵니다. 다음 프리뷰부터는 `studio` 폴더에서 `npm version prerelease --preid=preview --no-git-tag-version`으로 두 파일을 함께 갱신할 수 있습니다.
2. Windows x64 환경에 CPython 3.10 이상과 Node.js 22.12 이상을 준비합니다. 저장소 루트에서 다음 명령을 차례로 실행합니다. CI는 Python 3.12와 Node.js 24를 사용합니다.

```powershell
npm --prefix studio ci
node studio/node_modules/electron/install.js
python -m unittest discover -s tests
$version = (Get-Content -Raw studio/package.json | ConvertFrom-Json).version
python packaging/windows/build.py --studio-dist dist/studio --tag "studio-v$version"
```

3. `dist/studio`에 버전이 붙은 앱 폴더, ZIP, `.zip.sha256` 파일이 생깁니다. 같은 버전을 재검증할 때는 새 출력 폴더를 지정합니다. 공개할 빌드는 배포할 소스만 커밋된 체크아웃에서 생성합니다.

## 압축과 실행 확인

```powershell
$zip = Get-Item "dist/studio/Memdex-Studio-$version-Windows-x64.zip"
$expected = ((Get-Content -Raw "$($zip.FullName).sha256") -split '\s+')[0]
if ((Get-FileHash -Algorithm SHA256 $zip.FullName).Hash -ne $expected) { throw 'ZIP 체크섬 불일치' }
$check = Join-Path $env:TEMP ("memdex-studio-" + [guid]::NewGuid())
Expand-Archive -LiteralPath $zip.FullName -DestinationPath $check
$app = Get-Item "$check/Memdex-Studio-$version-Windows-x64/Memdex-Studio.exe"
& $app.FullName
```

앱 하단에서 Studio 버전을 확인합니다. 검토 기록이 있는 프로젝트에서 세 답변, 원문과 이전 시도, 창 폭을 줄였을 때의 설정 토글을 확인합니다. 테스트 프로젝트에서는 실패한 모델을 한 번 재시도해 나머지 모델의 시도 번호가 유지되는지 확인합니다. 터미널에서 `Get-Location`을 실행해 현재 작업 폴더와 비교합니다.

## GitHub 릴리스 절차

1. GitHub Actions의 **Build Memdex Studio**를 수동 실행하면 선택한 브랜치에서 ZIP과 체크섬을 빌드합니다. 워크플로에는 저장소 읽기 권한만 있습니다.
2. 프리뷰 공개가 결정되면 확인한 커밋에 `studio-v<Studio 버전>` 태그를 붙여 푸시합니다. 해당 태그로 같은 워크플로가 실행되며 버전이 다르면 빌드가 중단됩니다. Python CLI의 PyPI 릴리스는 별도 `v*` 태그와 워크플로를 사용합니다.
3. 태그 빌드의 **Memdex-Studio-Windows-x64** 아티팩트를 내려받아 바깥쪽 Actions ZIP을 풉니다. 안의 제품 ZIP과 체크섬으로 위 검증을 반복합니다.
4. GitHub Releases에서 같은 태그로 초안을 만들고 검증한 제품 ZIP과 `.zip.sha256`을 첨부합니다. 변경점과 지원 OS를 적고 **Set as a pre-release**를 선택해 검토한 뒤 게시합니다.

## 글꼴

한글 UI에는 Pretendard Variable v1.3.9, 코드와 터미널에는 IBM Plex Mono Regular를 포함합니다. 둘 다 SIL Open Font License 1.1에 따라 상업용 앱에 함께 배포할 수 있습니다. 저작권과 라이선스 원문은 ZIP 안의 `resources/app/fonts`에 있습니다. [Pretendard 원본 라이선스](https://github.com/orioncactus/pretendard/blob/v1.3.9/LICENSE) · [IBM Plex 원본 라이선스](https://github.com/IBM/plex/blob/bc6a7e4b37634eb79785d59781893db63ee3edcb/packages/plex-mono/LICENSE.txt)
