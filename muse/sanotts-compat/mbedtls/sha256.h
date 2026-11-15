#pragma once
/* sanoTTS-jp 1.2.0 uses the removed Mbed TLS 3 SHA-256 entry points.
 * ESP-IDF 6 / Mbed TLS 4 exposes the same operation through PSA Crypto.
 * This private adapter is used only when compiling the pinned TTS library. */
#include "psa/crypto.h"
#include <stddef.h>
#define mbedtls_sha256_context sumi_sha256_context
#define mbedtls_sha256_init sumi_sha256_init
#define mbedtls_sha256_starts sumi_sha256_starts
#define mbedtls_sha256_update sumi_sha256_update
#define mbedtls_sha256_finish sumi_sha256_finish
#define mbedtls_sha256_free sumi_sha256_free
typedef struct { psa_hash_operation_t operation; } mbedtls_sha256_context;
static inline void mbedtls_sha256_init(mbedtls_sha256_context *ctx) {
    ctx->operation = (psa_hash_operation_t)PSA_HASH_OPERATION_INIT;
}
static inline int mbedtls_sha256_starts(mbedtls_sha256_context *ctx, int is224) {
    if (is224 || psa_crypto_init() != PSA_SUCCESS) return -1;
    return psa_hash_setup(&ctx->operation, PSA_ALG_SHA_256) == PSA_SUCCESS ? 0 : -1;
}
static inline int mbedtls_sha256_update(mbedtls_sha256_context *ctx, const unsigned char *p, size_t n) {
    return psa_hash_update(&ctx->operation, p, n) == PSA_SUCCESS ? 0 : -1;
}
static inline int mbedtls_sha256_finish(mbedtls_sha256_context *ctx, unsigned char out[32]) {
    size_t size = 0;
    return psa_hash_finish(&ctx->operation, out, 32, &size) == PSA_SUCCESS && size == 32 ? 0 : -1;
}
static inline void mbedtls_sha256_free(mbedtls_sha256_context *ctx) {
    psa_hash_abort(&ctx->operation);
}
