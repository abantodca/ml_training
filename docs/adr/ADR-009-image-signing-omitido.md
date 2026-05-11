# ADR-009: Image signing (cosign) omitido por ahora

- **Estado**: Accepted
- **Fecha**: 2026-05-11
- **Tags**: `security`, `cicd`, `supply-chain`

## Contexto

Una version anterior del `ci.yml` incluia un paso de `cosign sign`
(keyless con OIDC de GitHub) que firmaba la imagen Docker pusheada a
ECR. Al revisar, se decidio simplificar el workflow inicial. Estado
actual:

- La cadena de supply se protege parcialmente por:
  - `trivy` scan en `ci.yml` que falla si encuentra CVEs HIGH/CRITICAL.
  - `pip-audit` sobre `requirements.txt`.
  - `gitleaks` sobre el repo.
  - ECR `scan_on_push = true` (guia §4.2).
  - IAM role pineado por OIDC subject claim a `repo:org/repo:ref:refs/heads/main`
    (guia §7.0) — un fork malicioso no puede pushear.
- **Falta**: garantia criptografica de que la imagen que corre Batch
  fue construida por tu CI/CD y no introducida manualmente con
  permisos AWS comprometidos.

## Opciones evaluadas

### Opcion A — `cosign sign` keyless en CI, sin enforcement
- **Pros**: firma criptografica por commit + identidad OIDC. Bajo
  costo de implementacion (1 paso YAML). Auditable retroactivamente.
- **Contras**: si nadie verifica la firma, la garantia es decorativa.

### Opcion B — `cosign sign` + AWS Signer + Notation policy en job-def
- **Pros**: enforcement real. Una imagen no firmada NO puede correr.
- **Contras**: trabajo significativo (~1 sprint). Requiere setup de
  AWS Signer profile, Notation, signature verification policy en
  Batch. Riesgo de bloquear deploys mientras se valida.

### Opcion C — Omitir signing por ahora
- **Pros**: workflow simple, foco en validar que el pipeline funciona
  end-to-end primero.
- **Contras**: sin garantia criptografica de origen.

## Decision

**Opcion C** por ahora. El paso de cosign NO esta en `ci.yml` §7.1.
La guia incluye una nota explicita explicando como activarlo
(`sigstore/cosign-installer@v3` + `cosign sign --yes <digest>`).

La defensa actual se apoya en:
1. OIDC role pineado por sub-claim (un actor externo NO puede pushear
   a ECR).
2. Trivy + pip-audit + gitleaks (detectan vulnerabilidades conocidas).
3. ECR scan_on_push (segunda capa de scan).
4. Branch protection en `main` (codigo entra solo via PR review).

## Consecuencias

**Positivas**:
- Workflow simple y rapido. CI/CD se valida primero, hardening despues.
- Sin riesgo de bloqueo en deploys por verificacion de firma fallida.

**Negativas / costos**:
- Si las creds de AWS de CI son comprometidas, un atacante puede
  pushear una imagen maliciosa a ECR sin que nadie lo detecte por
  firma. Mitigacion parcial: OIDC sub-claim pin + trivy scan + branch
  protection.
- Compliance frameworks (SLSA L3+, SSDF) exigen image signing —
  bloqueante si el negocio necesita certificarse.

**Migracion para activar Opcion A (signing sin enforcement)**:
Agregar al final del job `build` en `ci.yml`:
```yaml
      - uses: sigstore/cosign-installer@v3
      - run: |
          cosign sign --yes \
            ${{ steps.ecr.outputs.registry }}/${{ env.ECR_REPOSITORY }}@${{ steps.push.outputs.digest }}
        env: { COSIGN_EXPERIMENTAL: "1" }
```
Costo: ~1 dia (incluye configurar Fulcio + Rekor trust).

**Migracion para activar Opcion B (signing + enforcement)**:
Adicional a Opcion A:
1. Crear AWS Signer profile y notation policy.
2. Configurar `signatureVerificationPolicies` en la job-def Batch.
3. Validar que el flujo SUCCEEDED/FAILED funciona end-to-end con
   firmas validas/invalidas.

Costo: ~1 sprint.

## Trigger para reconsiderar

- Auditoria de compliance (PCI-DSS, HIPAA, SOC2, SLSA, SSDF) lo
  requiere.
- Incidente de cadena de supply en cualquier dependencia critica
  (mlflow, xgboost, lightgbm, scikit-learn).
- Equipo crece a >5 personas con acceso a CI.

## Verificacion

- `ci.yml` job `build` completa sin paso de cosign.
- `trivy` scan corre y falla en HIGH/CRITICAL — verifica que el gate
  de seguridad principal NO depende del signing.
