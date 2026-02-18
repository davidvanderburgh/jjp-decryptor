"""Embedded C sources for the JJP decryptor and stub libraries."""

# Minimal stub C source - just enough for the linker
STUB_C_SOURCE = """\
void __stub_placeholder(void) {}
"""

# The main decryptor C source - based on proven gnr_decrypt.c with modifications:
# 1. Output path from JJP_OUTPUT_DIR env var (default /tmp/jjp_decrypted)
# 2. TOTAL_FILES count emitted after parsing fl.dat
# 3. Progress every 100 files instead of 500
# 4. fl_decrypted.dat saved to output dir
DECRYPT_C_SOURCE = r"""
/*
 * jjp_decrypt.c - Universal JJP game asset decryptor
 *
 * Algorithm:
 * 1. Hook fm_process_filelist, let game parse fl.dat
 * 2. Re-decrypt fl.dat with dongle_decrypt_buffer
 * 3. Parse entries, decrypt each file with set_seeds_for_crypto + LE rand64 XOR
 * 4. Skip filler bytes, write content
 *
 * Addresses are found via dlsym (game-independent).
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <dlfcn.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>

typedef const char* (*fn_path)(void);
typedef void (*fn_set_crypto)(const char *);
typedef uint64_t (*fn_rand64)(void);
typedef void (*fn_dongle_decrypt)(void *buf, unsigned int size);
typedef void (*fn_process_fl)(const char*, const char*);
typedef int (*fn_void_int)(void);
typedef void (*fn_void_void)(void);

static const uint8_t png_magic[]  = {0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A};
static const uint8_t webm_magic[] = {0x1A,0x45,0xDF,0xA3};
static const uint8_t ogg_magic[]  = {'O','g','g','S'};

#define HOOK_SIZE 14
static uint8_t orig_pfl[HOOK_SIZE];
static void *pfl_addr = NULL;
static fn_set_crypto g_set_crypto;
static fn_rand64 g_rand64;
static fn_dongle_decrypt g_dongle_decrypt;

static char g_edata_prefix[256] = "";
static char g_output_dir[4096] = "/tmp/jjp_decrypted";

static void *page_align(void *a) { return (void*)((uintptr_t)a & ~0xFFF); }
static void write_jmp(uint8_t *t, void *d) {
    mprotect(page_align(t), 0x2000, PROT_READ|PROT_WRITE|PROT_EXEC);
    t[0]=0xFF; t[1]=0x25; t[2]=t[3]=t[4]=t[5]=0;
    *(uint64_t*)(t+6) = (uint64_t)d;
    __builtin_ia32_sfence();
}

static void mkdirs(const char *path) {
    char tmp[4096];
    snprintf(tmp, sizeof(tmp), "%s", path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') { *p = '\0'; mkdir(tmp, 0755); *p = '/'; }
    }
    mkdir(tmp, 0755);
}

static void do_decrypt(const char *fl_path) {
    fprintf(stderr, "[decrypt] fl.dat path: %s\n", fl_path);

    /* Read output dir from environment */
    const char *env_out = getenv("JJP_OUTPUT_DIR");
    if (env_out && env_out[0])
        snprintf(g_output_dir, sizeof(g_output_dir), "%s", env_out);
    mkdirs(g_output_dir);

    FILE *f = fopen(fl_path, "rb");
    if (!f) {
        fprintf(stderr, "[decrypt] Cannot open fl.dat: %s\n", fl_path);
        syscall(SYS_exit_group, 1);
    }

    fseek(f, 0, SEEK_END);
    long fsize = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *data = malloc(fsize + 16);
    fread(data, 1, fsize, f);
    fclose(f);

    fprintf(stderr, "[decrypt] Decrypting fl.dat (%ld bytes)...\n", fsize);
    g_dongle_decrypt(data, (unsigned)fsize);

    /* Check if text */
    int printable = 1;
    for (int i = 0; i < 32 && i < fsize; i++) {
        if (data[i] != '\n' && data[i] != '\r' && data[i] != '\t' &&
            (data[i] < 0x20 || data[i] > 0x7e)) { printable = 0; break; }
    }

    if (!printable) {
        fprintf(stderr, "[decrypt] fl.dat decryption FAILED (not text)\n");
        free(data);
        syscall(SYS_exit_group, 1);
    }

    fprintf(stderr, "[decrypt] fl.dat decrypted OK. First line:\n  ");
    char *nl = memchr(data, '\n', fsize);
    if (nl) fwrite(data, 1, nl - (char*)data, stderr);
    fprintf(stderr, "\n");

    /* Count total files */
    int total_files = 0;
    for (long i = 0; i < fsize; i++) {
        if (data[i] == '\n') total_files++;
    }
    fprintf(stderr, "[decrypt] TOTAL_FILES=%d\n", total_files);

    /* Save decrypted fl.dat to output dir */
    {
        char fl_out[4096];
        snprintf(fl_out, sizeof(fl_out), "%s/fl_decrypted.dat", g_output_dir);
        FILE *out = fopen(fl_out, "wb");
        if (out) { fwrite(data, 1, fsize, out); fclose(out); }
    }
    /* Also save to /tmp for the batch phase */
    {
        FILE *out = fopen("/tmp/fl_decrypted.dat", "wb");
        if (out) { fwrite(data, 1, fsize, out); fclose(out); }
    }

    /* Detect edata prefix from first entry */
    {
        char first[4096];
        size_t flen = nl ? (size_t)(nl - (char*)data) : (fsize < 4095 ? fsize : 4095);
        memcpy(first, data, flen);
        first[flen] = '\0';
        char *edata = strstr(first, "/edata/");
        if (edata) {
            size_t plen = (edata - first) + 7;
            memcpy(g_edata_prefix, first, plen);
            g_edata_prefix[plen] = '\0';
            fprintf(stderr, "[decrypt] Detected edata prefix: '%s'\n", g_edata_prefix);
        }
    }

    /* Quick verify on first PNGs */
    fprintf(stderr, "\n[decrypt] === Verification ===\n");
    {
        char *line = (char*)data;
        char *end = (char*)data + fsize;
        int tested = 0;
        while (line < end && tested < 3) {
            char *lnl = memchr(line, '\n', end - line);
            if (!lnl) lnl = end;
            size_t len = lnl - line;
            if (len > 0 && line[len-1] == '\r') len--;

            char entry[4096];
            if (len > 0 && len < sizeof(entry)) {
                memcpy(entry, line, len);
                entry[len] = '\0';

                char *c1 = strrchr(entry, ','); if (!c1) goto next;
                *c1 = '\0';
                char *c2 = strrchr(entry, ','); if (!c2) goto next;
                *c2 = '\0';
                char *c3 = strrchr(entry, ','); if (!c3) goto next;
                *c3 = '\0';
                uint32_t n1 = (uint32_t)atol(c3 + 1);
                char *filepath = entry;

                const char *ext = strrchr(filepath, '.');
                if (ext && strcasecmp(ext, ".png") == 0) {
                    FILE *ef = fopen(filepath, "rb");
                    if (ef) {
                        fseek(ef, 0, SEEK_END);
                        long esize = ftell(ef);
                        fseek(ef, 0, SEEK_SET);
                        uint8_t *edata = malloc(esize);
                        fread(edata, 1, esize, ef);
                        fclose(ef);

                        g_set_crypto(filepath);
                        for (long pos = 0; pos < esize; pos += 8) {
                            uint64_t k = g_rand64();
                            for (int b = 0; b < 8 && pos + b < esize; b++)
                                edata[pos + b] ^= ((k >> (b * 8)) & 0xFF);
                        }

                        if (esize > n1 + 8 && memcmp(edata + n1, png_magic, 8) == 0)
                            fprintf(stderr, "  [OK] %s\n", filepath);
                        else
                            fprintf(stderr, "  [FAIL] %s (filler=%u)\n", filepath, n1);
                        free(edata);
                        tested++;
                    }
                }
            }
            next:
            line = lnl + 1;
        }
    }

    /* Batch decrypt */
    fprintf(stderr, "\n[decrypt] === BATCH DECRYPTION ===\n");
    {
        FILE *fl2 = fopen("/tmp/fl_decrypted.dat", "r");
        if (!fl2) { fprintf(stderr, "Cannot reopen fl\n"); goto done; }

        int total = 0, ok = 0, fail = 0, skip = 0;
        char ln[4096];

        while (fgets(ln, sizeof(ln), fl2)) {
            size_t len = strlen(ln);
            while (len > 0 && (ln[len-1] == '\n' || ln[len-1] == '\r'))
                ln[--len] = '\0';
            if (len == 0) continue;

            char *c1 = strrchr(ln, ','); if (!c1) continue; *c1 = '\0';
            char *c2 = strrchr(ln, ','); if (!c2) continue; *c2 = '\0';
            char *c3 = strrchr(ln, ','); if (!c3) continue; *c3 = '\0';
            uint32_t n1 = (uint32_t)atol(c3 + 1);
            char *fp = ln;

            FILE *ef = fopen(fp, "rb");
            if (!ef) { skip++; total++; continue; }
            fseek(ef, 0, SEEK_END);
            long esize = ftell(ef);
            fseek(ef, 0, SEEK_SET);
            if (esize <= n1) { fclose(ef); skip++; total++; continue; }
            uint8_t *edata = malloc(esize);
            fread(edata, 1, esize, ef);
            fclose(ef);

            g_set_crypto(fp);
            for (long pos = 0; pos < esize; pos += 8) {
                uint64_t k = g_rand64();
                for (int b = 0; b < 8 && pos + b < esize; b++)
                    edata[pos + b] ^= ((k >> (b * 8)) & 0xFF);
            }

            /* Build output path */
            const char *rel = fp;
            if (g_edata_prefix[0] && strncmp(fp, g_edata_prefix, strlen(g_edata_prefix)) == 0)
                rel = fp + strlen(g_edata_prefix);

            char outpath[4096];
            snprintf(outpath, sizeof(outpath), "%s/%s", g_output_dir, rel);

            char dir[4096];
            snprintf(dir, sizeof(dir), "%s", outpath);
            char *sl = strrchr(dir, '/');
            if (sl) { *sl = '\0'; mkdirs(dir); }

            FILE *of = fopen(outpath, "wb");
            if (of) {
                fwrite(edata + n1, 1, esize - n1, of);
                fclose(of);
                ok++;
            } else {
                fail++;
            }
            free(edata);
            total++;
            if (total % 100 == 0)
                fprintf(stderr, "  Progress: %d (ok=%d fail=%d skip=%d)\n",
                        total, ok, fail, skip);
        }
        fclose(fl2);

        fprintf(stderr, "\n=== BATCH COMPLETE ===\n");
        fprintf(stderr, "  Total: %d  OK: %d  Failed: %d  Skipped: %d\n",
                total, ok, fail, skip);
    }

done:
    free(data);
    syscall(SYS_exit_group, 0);
}

typedef int (*fn_al_install)(int, int (*)(void (*)(void)));

int al_install_system(int version, int (*atexit_ptr)(void (*)(void))) {
    signal(SIGPIPE, SIG_IGN);
    void *h = dlopen(NULL, RTLD_NOW);

    fprintf(stderr, "[decrypt] Finding functions...\n");
    g_set_crypto = (fn_set_crypto)dlsym(h, "_Z27jcrypt_set_seeds_for_cryptoPKc");
    g_rand64 = (fn_rand64)dlsym(h, "_Z13jcrypt_rand64v");
    g_dongle_decrypt = (fn_dongle_decrypt)dlsym(h, "_Z21dongle_decrypt_bufferPvj");
    pfl_addr = dlsym(h, "_Z19fm_process_filelistPKcS0_");

    fprintf(stderr, "  set_seeds_for_crypto = %p\n", (void*)g_set_crypto);
    fprintf(stderr, "  rand64 = %p\n", (void*)g_rand64);
    fprintf(stderr, "  dongle_decrypt = %p\n", (void*)g_dongle_decrypt);
    fprintf(stderr, "  fm_process_filelist = %p\n", pfl_addr);

    if (!g_set_crypto || !g_rand64 || !g_dongle_decrypt) {
        fprintf(stderr, "[decrypt] Missing critical crypto functions!\n");
        syscall(SYS_exit_group, 1);
    }
    if (!pfl_addr) {
        fprintf(stderr, "[decrypt] Warning: fm_process_filelist not found (non-critical)\n");
    }

    fprintf(stderr, "[decrypt] All functions found.\n");

    /* The dongle_decrypt_buffer function needs an active HASP session.
     * Search for and call the dongle initialization function to establish
     * the HASP license session before we try to decrypt fl.dat. */
    {
        void *dinit = NULL;
        /* Try common mangled C++ names for dongle init functions */
        const char *init_names[] = {
            "_Z11dongle_initv",           /* dongle_init() */
            "_Z11dongle_initb",           /* dongle_init(bool) */
            "_Z17dongle_initializev",     /* dongle_initialize() */
            "_Z14dongle_connectv",        /* dongle_connect() */
            "_Z12dongle_loginv",          /* dongle_login() */
            "_Z10DongleInitv",            /* DongleInit() */
            "_Z11dongle_initRKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEE", /* dongle_init(std::string const&) */
            "dongle_init",                /* extern "C" */
            "dongle_initialize",
            NULL
        };
        for (int i = 0; init_names[i]; i++) {
            dinit = dlsym(h, init_names[i]);
            if (dinit) {
                fprintf(stderr, "[decrypt] Found dongle init: %s @ %p\n",
                        init_names[i], dinit);
                break;
            }
        }

        if (dinit) {
            fprintf(stderr, "[decrypt] Calling dongle init...\n");
            /* Try calling as void->int first (most common) */
            int ret = ((fn_void_int)dinit)();
            fprintf(stderr, "[decrypt] Dongle init returned: %d\n", ret);
        } else {
            fprintf(stderr, "[decrypt] WARNING: No dongle init function found!\n");
            fprintf(stderr, "[decrypt] Will attempt decryption anyway...\n");
        }
    }

    /* Find fl.dat from game binary path via /proc/self/exe */
    char exe_path[4096];
    ssize_t elen = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
    if (elen <= 0) {
        fprintf(stderr, "[decrypt] Cannot read /proc/self/exe\n");
        syscall(SYS_exit_group, 1);
    }
    exe_path[elen] = '\0';
    fprintf(stderr, "[decrypt] Game binary: %s\n", exe_path);

    /* Get game directory (dirname of game binary) */
    char *slash = strrchr(exe_path, '/');
    if (slash) *slash = '\0';

    /* Search for fl.dat in common locations */
    char fl_path[4096];
    FILE *fl_test = NULL;
    const char *fl_locations[] = {
        "%s/edata/fl.dat",
        "%s/fl.dat",
        "%s/data/fl.dat",
        NULL
    };
    for (int i = 0; fl_locations[i]; i++) {
        snprintf(fl_path, sizeof(fl_path), fl_locations[i], exe_path);
        fl_test = fopen(fl_path, "rb");
        if (fl_test) { fclose(fl_test); break; }
    }

    if (!fl_test) {
        fprintf(stderr, "[decrypt] Cannot find fl.dat in %s\n", exe_path);
        syscall(SYS_exit_group, 1);
    }

    fprintf(stderr, "[decrypt] Found fl.dat: %s\n", fl_path);
    fprintf(stderr, "[decrypt] Running decryption directly (headless mode).\n");
    do_decrypt(fl_path);

    /* do_decrypt exits via syscall(SYS_exit_group, 0) */
    return 1;
}

__attribute__((constructor))
static void init(void) { signal(SIGPIPE, SIG_IGN); }
"""


# The encryptor C source - re-encrypts replacement assets into the game image.
# Uses the same XOR crypto as decryption (symmetric), with round-trip verification.
ENCRYPT_C_SOURCE = r"""
/*
 * jjp_encrypt.c - JJP game asset re-encryptor
 *
 * Algorithm:
 * 1. Hook al_install_system, init dongle session
 * 2. Decrypt fl.dat to get filler counts per file
 * 3. Read manifest of (relative_path, replacement_path) pairs
 * 4. For each: prepend filler, XOR-encrypt, overwrite original
 * 5. Round-trip verify each file
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <dlfcn.h>
#include <sys/stat.h>
#include <sys/syscall.h>

typedef void (*fn_set_crypto)(const char *);
typedef uint64_t (*fn_rand64)(void);
typedef void (*fn_dongle_decrypt)(void *buf, unsigned int size);
typedef int (*fn_void_int)(void);

static fn_set_crypto g_set_crypto;
static fn_rand64 g_rand64;
static fn_dongle_decrypt g_dongle_decrypt;

static char g_edata_prefix[256] = "";

/* Linked list for fl.dat entries */
typedef struct fl_entry {
    char path[4096];
    uint32_t n1;
    struct fl_entry *next;
} fl_entry;

static void do_encrypt(const char *fl_path) {
    fprintf(stderr, "[encrypt] fl.dat path: %s\n", fl_path);

    /* Read and decrypt fl.dat */
    FILE *f = fopen(fl_path, "rb");
    if (!f) {
        fprintf(stderr, "[encrypt] Cannot open fl.dat: %s\n", fl_path);
        syscall(SYS_exit_group, 1);
    }
    fseek(f, 0, SEEK_END);
    long fsize = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *fldata = malloc(fsize + 16);
    fread(fldata, 1, fsize, f);
    fclose(f);

    fprintf(stderr, "[encrypt] Decrypting fl.dat (%ld bytes)...\n", fsize);
    g_dongle_decrypt(fldata, (unsigned)fsize);

    /* Check if decryption produced text */
    int printable = 1;
    for (int i = 0; i < 32 && i < fsize; i++) {
        if (fldata[i] != '\n' && fldata[i] != '\r' && fldata[i] != '\t' &&
            (fldata[i] < 0x20 || fldata[i] > 0x7e)) { printable = 0; break; }
    }
    if (!printable) {
        fprintf(stderr, "[encrypt] fl.dat decryption FAILED (not text)\n");
        free(fldata);
        syscall(SYS_exit_group, 1);
    }
    fprintf(stderr, "[encrypt] fl.dat decrypted OK.\n");

    /* Detect edata prefix from first entry */
    {
        char *nl = memchr(fldata, '\n', fsize);
        char first[4096];
        size_t flen = nl ? (size_t)(nl - (char*)fldata) : (fsize < 4095 ? fsize : 4095);
        memcpy(first, fldata, flen);
        first[flen] = '\0';
        char *edata = strstr(first, "/edata/");
        if (edata) {
            size_t plen = (edata - first) + 7;
            memcpy(g_edata_prefix, first, plen);
            g_edata_prefix[plen] = '\0';
            fprintf(stderr, "[encrypt] Detected edata prefix: '%s'\n", g_edata_prefix);
        }
    }

    /* Parse fl.dat into lookup list */
    fl_entry *fl_head = NULL;
    int fl_count = 0;
    {
        char *line = (char*)fldata;
        char *end = (char*)fldata + fsize;
        while (line < end) {
            char *nl = memchr(line, '\n', end - line);
            if (!nl) nl = end;
            size_t len = nl - line;
            if (len > 0 && line[len-1] == '\r') len--;
            if (len > 0 && len < 4096) {
                char entry[4096];
                memcpy(entry, line, len);
                entry[len] = '\0';
                char *c1 = strrchr(entry, ','); if (!c1) goto next;
                *c1 = '\0';
                char *c2 = strrchr(entry, ','); if (!c2) goto next;
                *c2 = '\0';
                char *c3 = strrchr(entry, ','); if (!c3) goto next;
                *c3 = '\0';
                uint32_t n1 = (uint32_t)atol(c3 + 1);
                fl_entry *e = malloc(sizeof(fl_entry));
                strncpy(e->path, entry, 4095);
                e->path[4095] = '\0';
                e->n1 = n1;
                e->next = fl_head;
                fl_head = e;
                fl_count++;
            }
            next:
            line = nl + 1;
        }
    }
    fprintf(stderr, "[encrypt] Parsed %d entries from fl.dat\n", fl_count);
    free(fldata);

    /* Read manifest file */
    const char *manifest_path = getenv("JJP_MANIFEST");
    if (!manifest_path || !manifest_path[0])
        manifest_path = "/tmp/jjp_manifest.txt";

    FILE *mf = fopen(manifest_path, "r");
    if (!mf) {
        fprintf(stderr, "[encrypt] Cannot open manifest: %s\n", manifest_path);
        syscall(SYS_exit_group, 1);
    }

    /* Count entries */
    int total = 0;
    char mline[8192];
    while (fgets(mline, sizeof(mline), mf)) {
        size_t len = strlen(mline);
        while (len > 0 && (mline[len-1] == '\n' || mline[len-1] == '\r'))
            mline[--len] = '\0';
        if (len > 0) total++;
    }
    fseek(mf, 0, SEEK_SET);
    fprintf(stderr, "[encrypt] TOTAL_FILES=%d\n", total);

    int ok = 0, fail = 0, processed = 0;

    while (fgets(mline, sizeof(mline), mf)) {
        size_t len = strlen(mline);
        while (len > 0 && (mline[len-1] == '\n' || mline[len-1] == '\r'))
            mline[--len] = '\0';
        if (len == 0) continue;

        /* Parse: game_relative_path\treplacement_path */
        char *tab = strchr(mline, '\t');
        if (!tab) {
            fprintf(stderr, "[encrypt] [FAIL] Bad manifest line: %s\n", mline);
            fail++; processed++; continue;
        }
        *tab = '\0';
        char *rel_path = mline;
        char *repl_path = tab + 1;

        /* Construct full game path */
        char full_path[4096];
        snprintf(full_path, sizeof(full_path), "%s%s", g_edata_prefix, rel_path);

        fprintf(stderr, "[encrypt] Processing: %s\n", full_path);

        /* Look up filler count in fl.dat */
        uint32_t n1 = 0;
        int found = 0;
        for (fl_entry *e = fl_head; e; e = e->next) {
            if (strcmp(e->path, full_path) == 0) {
                n1 = e->n1; found = 1; break;
            }
        }
        /* Fallback: try path as-is (might already be absolute) */
        if (!found) {
            for (fl_entry *e = fl_head; e; e = e->next) {
                if (strcmp(e->path, rel_path) == 0) {
                    snprintf(full_path, sizeof(full_path), "%s", rel_path);
                    n1 = e->n1; found = 1; break;
                }
            }
        }
        if (!found) {
            fprintf(stderr, "[encrypt] [FAIL] %s (not found in fl.dat)\n", rel_path);
            fail++; processed++; continue;
        }

        /* Read replacement file */
        FILE *rf = fopen(repl_path, "rb");
        if (!rf) {
            fprintf(stderr, "[encrypt] [FAIL] %s (cannot read: %s)\n",
                    rel_path, repl_path);
            fail++; processed++; continue;
        }
        fseek(rf, 0, SEEK_END);
        long rsize = ftell(rf);
        fseek(rf, 0, SEEK_SET);
        uint8_t *rdata = malloc(rsize);
        fread(rdata, 1, rsize, rf);
        fclose(rf);

        /* Build buffer: filler (zeros) + replacement data */
        long total_size = (long)n1 + rsize;
        uint8_t *buf = calloc(1, total_size);
        memcpy(buf + n1, rdata, rsize);

        /* XOR-encrypt */
        g_set_crypto(full_path);
        for (long pos = 0; pos < total_size; pos += 8) {
            uint64_t k = g_rand64();
            for (int b = 0; b < 8 && pos + b < total_size; b++)
                buf[pos + b] ^= ((k >> (b * 8)) & 0xFF);
        }

        /* Write encrypted data over original file */
        FILE *of = fopen(full_path, "wb");
        if (!of) {
            fprintf(stderr, "[encrypt] [FAIL] %s (cannot write)\n", rel_path);
            free(buf); free(rdata);
            fail++; processed++; continue;
        }
        fwrite(buf, 1, total_size, of);
        fclose(of);
        free(buf);

        /* === Round-trip verification === */
        FILE *vf = fopen(full_path, "rb");
        if (!vf) {
            fprintf(stderr, "[encrypt] [VERIFY FAIL] %s (cannot re-read)\n", rel_path);
            free(rdata); fail++; processed++; continue;
        }
        fseek(vf, 0, SEEK_END);
        long vsize = ftell(vf);
        fseek(vf, 0, SEEK_SET);
        uint8_t *vdata = malloc(vsize);
        fread(vdata, 1, vsize, vf);
        fclose(vf);

        /* Decrypt the just-written file */
        g_set_crypto(full_path);
        for (long pos = 0; pos < vsize; pos += 8) {
            uint64_t k = g_rand64();
            for (int b = 0; b < 8 && pos + b < vsize; b++)
                vdata[pos + b] ^= ((k >> (b * 8)) & 0xFF);
        }

        /* Compare payload (skip filler) */
        int verify_ok = 1;
        if (vsize != total_size) {
            verify_ok = 0;
        } else if (rsize > 0 && memcmp(vdata + n1, rdata, rsize) != 0) {
            verify_ok = 0;
        }

        if (verify_ok) {
            fprintf(stderr, "[encrypt] [VERIFY OK] %s\n", rel_path);
            ok++;
        } else {
            fprintf(stderr, "[encrypt] [VERIFY FAIL] %s\n", rel_path);
            fail++;
        }

        free(vdata);
        free(rdata);
        processed++;
        fprintf(stderr, "  Progress: %d (ok=%d fail=%d)\n", processed, ok, fail);
    }
    fclose(mf);

    /* Free fl entries */
    while (fl_head) {
        fl_entry *tmp = fl_head;
        fl_head = fl_head->next;
        free(tmp);
    }

    fprintf(stderr, "\n=== ENCRYPT COMPLETE ===\n");
    fprintf(stderr, "  Total: %d  OK: %d  Failed: %d\n", processed, ok, fail);
    syscall(SYS_exit_group, fail > 0 ? 1 : 0);
}

typedef int (*fn_al_install)(int, int (*)(void (*)(void)));

int al_install_system(int version, int (*atexit_ptr)(void (*)(void))) {
    signal(SIGPIPE, SIG_IGN);
    void *h = dlopen(NULL, RTLD_NOW);

    fprintf(stderr, "[encrypt] Finding functions...\n");
    g_set_crypto = (fn_set_crypto)dlsym(h, "_Z27jcrypt_set_seeds_for_cryptoPKc");
    g_rand64 = (fn_rand64)dlsym(h, "_Z13jcrypt_rand64v");
    g_dongle_decrypt = (fn_dongle_decrypt)dlsym(h, "_Z21dongle_decrypt_bufferPvj");

    fprintf(stderr, "  set_seeds_for_crypto = %p\n", (void*)g_set_crypto);
    fprintf(stderr, "  rand64 = %p\n", (void*)g_rand64);
    fprintf(stderr, "  dongle_decrypt = %p\n", (void*)g_dongle_decrypt);

    if (!g_set_crypto || !g_rand64 || !g_dongle_decrypt) {
        fprintf(stderr, "[encrypt] Missing critical crypto functions!\n");
        syscall(SYS_exit_group, 1);
    }
    fprintf(stderr, "[encrypt] All functions found.\n");

    /* Initialize HASP dongle session */
    {
        void *dinit = NULL;
        const char *init_names[] = {
            "_Z11dongle_initv",
            "_Z11dongle_initb",
            "_Z17dongle_initializev",
            "_Z14dongle_connectv",
            "_Z12dongle_loginv",
            "_Z10DongleInitv",
            "_Z11dongle_initRKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEE",
            "dongle_init",
            "dongle_initialize",
            NULL
        };
        for (int i = 0; init_names[i]; i++) {
            dinit = dlsym(h, init_names[i]);
            if (dinit) {
                fprintf(stderr, "[encrypt] Found dongle init: %s @ %p\n",
                        init_names[i], dinit);
                break;
            }
        }
        if (dinit) {
            fprintf(stderr, "[encrypt] Calling dongle init...\n");
            int ret = ((fn_void_int)dinit)();
            fprintf(stderr, "[encrypt] Dongle init returned: %d\n", ret);
        } else {
            fprintf(stderr, "[encrypt] WARNING: No dongle init function found!\n");
            fprintf(stderr, "[encrypt] Will attempt anyway...\n");
        }
    }

    /* Find fl.dat */
    char exe_path[4096];
    ssize_t elen = readlink("/proc/self/exe", exe_path, sizeof(exe_path) - 1);
    if (elen <= 0) {
        fprintf(stderr, "[encrypt] Cannot read /proc/self/exe\n");
        syscall(SYS_exit_group, 1);
    }
    exe_path[elen] = '\0';
    fprintf(stderr, "[encrypt] Game binary: %s\n", exe_path);

    char *slash = strrchr(exe_path, '/');
    if (slash) *slash = '\0';

    char fl_path[4096];
    FILE *fl_test = NULL;
    const char *fl_locations[] = {
        "%s/edata/fl.dat", "%s/fl.dat", "%s/data/fl.dat", NULL
    };
    for (int i = 0; fl_locations[i]; i++) {
        snprintf(fl_path, sizeof(fl_path), fl_locations[i], exe_path);
        fl_test = fopen(fl_path, "rb");
        if (fl_test) { fclose(fl_test); break; }
    }
    if (!fl_test) {
        fprintf(stderr, "[encrypt] Cannot find fl.dat in %s\n", exe_path);
        syscall(SYS_exit_group, 1);
    }

    fprintf(stderr, "[encrypt] Found fl.dat: %s\n", fl_path);
    fprintf(stderr, "[encrypt] Running encryption.\n");
    do_encrypt(fl_path);

    return 1;
}

__attribute__((constructor))
static void init(void) { signal(SIGPIPE, SIG_IGN); }
"""
