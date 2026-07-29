# Monitor de Chuva - Unidades Cocal

Automação pessoal que cruza **três fontes** de previsão do tempo e avisa por e-mail
sobre chuva nas unidades. Roda sozinha no GitHub Actions, várias vezes ao dia.

## O que ela faz

- Consulta **Yr / MET Norway**, **Open-Meteo** e **OpenWeatherMap** para cada unidade.
- Considera chuva como **FATO** só quando **2 ou mais fontes concordam** no mesmo período
  do dia (madrugada / manhã / tarde / noite). Se só 1 aponta, aparece como "possível".
- Envia e-mail (só para o seu endereço pessoal) em três situações:
  1. **Segunda-feira de manhã** → relatório da semana (o mais importante).
  2. **Véspera** de um dia com chuva confirmada → lembrete.
  3. **Mudança** na previsão para amanhã (passou a chover, ou não chove mais) → alerta.
- Guarda o estado em `estado.json` para detectar mudanças entre execuções.

## Como colocar para rodar (uma vez só)

1. **Crie um repositório** no GitHub (pode ser privado) e suba estes arquivos:
   `monitor_chuva.py`, `requirements.txt` e a pasta `.github/workflows/chuva.yml`.
2. **Pegue a chave gratuita do OpenWeatherMap:** crie conta em
   https://openweathermap.org/api → menu "API keys" → copie a chave.
   (A chave leva ~1-2 h para ativar depois do cadastro.)
3. **Crie a conta de envio (Gmail):** ative a verificação em 2 etapas e gere uma
   "senha de app" em https://myaccount.google.com/apppasswords
4. **Cadastre os segredos** no repositório em
   *Settings → Secrets and variables → Actions → New repository secret*:
   - `EMAIL_SENHA` → a senha de app do Gmail.
   - `OWM_API_KEY` → a chave do OpenWeatherMap.
5. **Edite o topo do `monitor_chuva.py`** com seus dados reais: coordenadas das unidades,
   `USER_AGENT` (seu e-mail), `EMAIL_REMETENTE` e `EMAIL_DESTINATARIO`.
6. Pronto. Na aba **Actions** você pode clicar em **Run workflow** para testar na hora.
   Para forçar o relatório semanal num teste fora de segunda, adicione um secret
   `FORCAR_RESUMO` com valor `1` (e remova depois).

## Observações

- **Uso pessoal:** o e-mail vai só para a sua conta pessoal; você repassa manualmente a
  quem quiser. É o que mantém o uso das APIs dentro do gratuito.
- **OpenWeatherMap** cobre 5 dias (não 7). Nos dias 6 e 7 o cruzamento usa Yr + Open-Meteo.
- Se uma fonte cair ou a chave faltar, o programa segue com as outras duas sem quebrar.
- **Créditos dos dados:** MET Norway, Open-Meteo (CC BY 4.0) e OpenWeatherMap.
