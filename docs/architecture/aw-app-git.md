---
repo: architecture
path: docs/architecture/aw-app-git.md
source: generated
edited: false
checksum: sha256:8e4eda3c13c7170d97d024f6f171518025d4b0051e2196a87f0d93d6e3c22a71
---
# Git + GitHub CLI

- **repo**: aw-app-git
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Installs git and the GitHub CLI (gh) into the workspace, survives restarts, provides a settings panel for gh login (token stored in the zero-knowledge secret store), and serves the GitHub PR dashboard (open PRs for you and your team, plus repo status) the workspace nav reads.

## Connections
- `http` → **aw-workspace** — routes mounted at /api/apps/git
- `other` → **aw-app-diff-tool** — The repos nav's "show diff" arrow opens aw-app-diff-tool's window (POST /api/apps/diff-tool/diffs/render)

## MCP tools
_none exposed_

## Requirements
### O espelho de credencial é escrito no lugar, nunca substituído
- Given containers de agente já estão com os arquivos de ~/.config/gh e .gitconfig do data dir deste app bind-montados
- When um novo login espelha as credenciais para o data dir (repos/aw-app-git/git_app/gh_auth.py::_sync_creds_to_data_dir:69, escrita em ::_write_in_place:43)
- Then cada arquivo é truncado e reescrito no mesmo inode, e os containers já rodando passam a ver a credencial nova — se fosse rmtree+copytree o bind ficaria apontando para um inode apagado e todo agente já de pé continuaria com a credencial velha (ou nenhuma) até ser recriado, sem nada indicar que o login novo não chegou
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-git/tests/test_gh_auth.py` (passing)

### Logout esvazia o espelho em vez de apagá-lo
- Given um login foi revogado e o data dir ainda tem a cópia das credenciais que containers de agente montam
- When o logout reverte o espelho (repos/aw-app-git/git_app/gh_auth.py::_clear_creds_from_data_dir:106)
- Then os arquivos passam a existir vazios e o container de agente lê um gh config vazio e reporta corretamente "not logged in" — apagar deixaria o mount apontando para inode morto, e um agente que ainda conseguisse ler a cópia antiga seguiria usando uma credencial revogada sem que o logout tivesse qualquer efeito visível
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-git/tests/test_gh_auth.py` (passing)

### Escopo amplo conta pelos escopos estreitos que implica
- Given um token do GitHub concede repo, user ou admin:public_key, que na prática já incluem public_repo, read:user, write:public_key e afins
- When o painel calcula missing_scopes a partir do header X-OAuth-Scopes (repos/aw-app-git/git_app/gh_auth.py::token_info:300, expansão em ::_effective_scopes:292)
- Then os implicados entram na conta e um token completo reporta lista vazia, enquanto um token só de repo reporta exatamente o que falta — sem a expansão o painel acusa escopo faltando num token que funciona, e a pessoa refaz o device flow atrás de um problema que não existe em vez de olhar a causa real
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-git/tests/test_gh_auth.py` (passing)

### O watchdog de não-commitado só avisa quando o conjunto sujo muda
- Given um repo do workspace fica com alterações não commitadas por vários ciclos do watchdog
- When o tick compara o mapa repo→arquivos sujos com o do ciclo anterior (repos/aw-app-git/git_app/uncommitted_watchdog.py::UncommittedWatchdog.tick:85)
- Then um estado idêntico não dispara notificação nenhuma, e um repo novo sujo ou uma lista de arquivos diferente dispara — sem o diff o mesmo repo esquecido notifica a cada ciclo, e o canal vira ruído que a pessoa passa a ignorar exatamente quando aparece a mudança que importava
- intended_status: `not_implemented` · derived health: `not_implemented`
- tests: `repos/aw-app-git/tests/test_uncommitted_watchdog.py` (passing)
