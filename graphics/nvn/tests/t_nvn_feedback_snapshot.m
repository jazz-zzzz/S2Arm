// Real Metal functions, production NVN draw/argument encoding/feedback copies,
// and independent GPU readback. No replaced production or host symbols.
// NVN_FEEDBACK_SOURCE permits compiling an exact git-show copy of nvn_mtl.m
// without changing the live source used by another contributor.
#ifndef NVN_FEEDBACK_SOURCE
#define NVN_FEEDBACK_SOURCE "../gfx/nvn/nvn_mtl.m"
#endif
#include NVN_FEEDBACK_SOURCE

static unsigned checks, failures;
#define REQUIRE(c) do { if (!(c)) { fprintf(stderr, "SETUP FAIL %s:%d %s\n", __FILE__, __LINE__, #c); exit(2); } } while (0)
#define CHECK(c, ...) do { checks++; int ok = !!(c); failures += !ok; printf("%s ", ok ? "PASS" : "FAIL"); printf(__VA_ARGS__); putchar('\n'); fflush(stdout); } while (0)

static NSString *const source = @"#include <metal_stdlib>\nusing namespace metal;\n"
"struct Inputs { texture2d<float, access::read> a [[id(0)]]; texture2d<float, access::read> b [[id(1)]]; };\n"
"vertex float4 triangle(uint i [[vertex_id]]) { const float2 p[3] = {float2(-1,-1),float2(3,-1),float2(-1,3)}; return float4(p[i],0,1); }\n"
"struct Result { float4 c [[color(2)]]; };\n"
"fragment Result colors(constant Inputs &t [[buffer(0)]]) { float4 a=t.a.read(uint2(0)), b=t.b.read(uint2(0)); Result o; o.c=float4(a.r,b.r,a.g,1); return o; }\n";

static NvShader *shader(id<MTLLibrary> library, NSString *name, int stage) {
    NvShader *s = [NvShader new]; s->stage = stage;
    s->fn = [library newFunctionWithName:name]; REQUIRE(s->fn);
    strlcpy(s->key, stage ? "bbbbbbbbbbbbbbbb_b" : "aaaaaaaaaaaaaaaa_b", sizeof s->key);
    s->rf.pos_attr = -1;
    s->rf.has_user = stage ? 2 : 1;
    if (stage) {
        // These two bindings correspond exactly to Inputs' id(0)/id(1),
        // texture2d<float,access::read> and the buffer(0) declaration above.
        s->rf.nb = 2; s->rf.abi = 0;
        s->rf.b = calloc(2, sizeof *s->rf.b); REQUIRE(s->rf.b);
        for (int i = 0; i < 2; i++) s->rf.b[i] = (nvn_rbind){.kind=NVN_RB_TEX, .reg=i, .aid=i, .dtt=MTLTextureType2D};
        s->ae = [s->fn newArgumentEncoderWithBufferIndex:0]; REQUIRE(s->ae);
        s->arg_len = s->ae.encodedLength;
        s->rf.color_known = 1; s->rf.color_mask = 1 << 2;
    }
    return s;
}

static void make_texture(tex_t *t, pool_t *pool, size_t offset) {
    memset(t, 0, sizeof *t); t->magic = MAGIC('T','E','X',0); t->serial = nvn_new_serial();
    t->d = (nvn_texdesc){.target=NVN_TEXTURE_TARGET_2D, .format=NVN_FORMAT_RGBA8,
        .flags=NVN_TEXTURE_FLAGS_IMAGE, .width=4, .height=4, .depth=1, .levels=1};
    t->pool=pool; t->off=offset; t->mem=pool->mem+offset;
    nvn_tex_layout(&t->d, &t->L); nvn_be_tex_init(t);
    id<MTLTexture> image=tex_mtl(t); REQUIRE(image);
    REQUIRE(image.pixelFormat == MTLPixelFormatRGBA8Unorm);
}

static void clear(tex_t *t, unsigned r, unsigned g, unsigned b) {
    p_clear_tex c={.tex=t, .value_type=CLEAR_TEX_FLOAT, .r={0,0,0,4,4,1}, .mask=15};
    c.c.f[0]=r/255.0f; c.c.f[1]=g/255.0f; c.c.f[2]=b/255.0f; c.c.f[3]=1;
    clear_tex(&c);
}

static void targets(tex_t *a, tex_t *b, tex_t *out) {
    rp_flush();
    memset(&R.rt, 0, sizeof R.rt); R.rt.n=3;
    R.rt.color[0]=a; R.rt.color[1]=b; R.rt.color[2]=out;
    R.has_rt=1; g_pso_dirty=1; args_dirty_all();
}

static void pixels(const char *name, tex_t *t, unsigned r, unsigned g, unsigned b) {
    id<MTLTexture> image=tex_mtl(t); REQUIRE(image);
    // Read the GPU result independently; no production readback/layout decoder.
    id<MTLBuffer> buffer=[g_dev newBufferWithLength:1024 options:MTLResourceStorageModeShared]; REQUIRE(buffer);
    memset(buffer.contents, 0xa5, 1024);
    id<MTLCommandQueue> queue=[g_dev newCommandQueue]; REQUIRE(queue);
    id<MTLCommandBuffer> cb=[queue commandBuffer]; REQUIRE(cb);
    id<MTLBlitCommandEncoder> enc=[cb blitCommandEncoder]; REQUIRE(enc);
    [enc copyFromTexture:image sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0)
        sourceSize:MTLSizeMake(4,4,1) toBuffer:buffer destinationOffset:0 destinationBytesPerRow:256 destinationBytesPerImage:1024];
    [enc endEncoding]; [cb commit]; [cb waitUntilCompleted]; REQUIRE(cb.status==MTLCommandBufferStatusCompleted);
    const uint8_t *bytes=buffer.contents; unsigned wrong=0;
    unsigned expected[4]={r,g,b,255};
    for(unsigned y=0;y<4;y++) for(unsigned x=0;x<4;x++) for(unsigned c=0;c<4;c++) {
        unsigned actual=bytes[y*256+x*4+c];
        if(actual!=expected[c]) wrong++;
    }
    CHECK(!wrong, "%s: 16 GPU pixels expected=(%u,%u,%u,255), first=(%u,%u,%u,%u), wrong channels=%u",
        name,r,g,b,bytes[0],bytes[1],bytes[2],bytes[3],wrong);
}

static void finish(void) {
    uint64_t fence=nvn_be_sub_commit();
    REQUIRE(nvn_be_wait(fence,10000000000ull)); REQUIRE(!mtl_last_commit_error());
}

static void pending_clear_draw(const p_draw *triangle) {
    struct {
        op_hdr h0; p_clear_color c0;
        op_hdr h1; p_clear_color c1;
        op_hdr hd; p_draw d;
        op_hdr end;
    } commands = {
        .h0={OP_CLEAR_COLOR,sizeof(op_hdr)+sizeof(p_clear_color)},
        .c0={.index=0,.mask=15,.rgba={48/255.0f,80/255.0f,112/255.0f,1}},
        .h1={OP_CLEAR_COLOR,sizeof(op_hdr)+sizeof(p_clear_color)},
        .c1={.index=1,.mask=15,.rgba={176/255.0f,208/255.0f,240/255.0f,1}},
        .hd={OP_DRAW,sizeof(op_hdr)+sizeof(p_draw)},.d=*triangle,
        .end={OP_END,sizeof(op_hdr)}
    };
    nvn_be_exec((const uint8_t *)&commands);
}

int main(void) {
    @autoreleasepool {
        char map[]="/tmp/t_feedback_map_XXXXXX"; int fd=mkstemp(map); REQUIRE(fd>=0); close(fd);
        char user[]="/tmp/t_feedback_user_XXXXXX"; fd=mkstemp(user); REQUIRE(fd>=0); close(fd);
        setenv("DM_NVN_SHMAP",map,1); setenv("DM_PSO_USER",user,1);
        setenv("DM_NVN_FB_SNAP","all",1);
        setenv("DM_PSO_ARCHIVE","0",1); // no shared /tmp pipeline cache; real PSOs still compile
        mtl_init(); printf("BACKEND %s\n",g_m3?"Metal3":"Metal4");
        // Replay's first entry initializes its state. Do so before installing
        // the bindings used by these direct production draw calls.
        const op_hdr empty={OP_END,sizeof(op_hdr)}; nvn_be_exec((const uint8_t *)&empty);
        NSError *error=nil; id<MTLLibrary> library=[g_dev newLibraryWithSource:source options:nil error:&error];
        if(!library) fprintf(stderr,"MSL: %s\n",error.localizedDescription.UTF8String); REQUIRE(library);
        NvProg *program=[NvProg new]; program->sh[0]=shader(library,@"triangle",0); program->sh[1]=shader(library,@"colors",1);
        prog_t p={.magic=MAGIC('P','R','O','G'), .serial=nvn_new_serial()}; serial_put(p.serial,program);
        uint8_t *memory=NULL; REQUIRE(!posix_memalign((void **)&memory,65536,1<<20)); memset(memory,0,1<<20);
        // Production no-copy pool remains alive for the complete GPU lifetime.
        pool_t *pool=calloc(1,sizeof *pool); REQUIRE(pool);
        *pool=(pool_t){.magic=MAGIC('P','O','O','L'), .flags=NVN_MEMORY_POOL_FLAGS_CPU_NO_ACCESS|NVN_MEMORY_POOL_FLAGS_GPU_CACHED, .mem=memory, .size=1<<20};
        nvn_be_pool_add(memory,1<<20);
        tex_t a,b,first,second; make_texture(&a,pool,0); make_texture(&b,pool,65536);
        make_texture(&first,pool,131072); make_texture(&second,pool,196608);
        nvn_be_reg_tex(1,&a,NULL); nvn_be_reg_tex(2,&b,NULL);
        p_draw triangle={.prim=NVN_DRAW_PRIMITIVE_TRIANGLES, .count=3, .instances=1};

        nvn_be_sub_begin(); state_reset(); S.prog=&p; S.tex[1][0]=1; S.tex[1][1]=2;
        clear(&a,32,64,96); clear(&b,160,192,224); clear(&first,0,0,0); clear(&second,0,0,0);
        targets(&a,&b,&first); draw(&triangle,NULL);
        // A second real draw in the same submission must copy the new content.
        clear(&a,80,112,144); clear(&b,208,16,48);
        targets(&a,&b,&second); draw(&triangle,NULL); finish();
        pixels("same draw, distinct same-format/size attachments",&first,32,160,64);
        pixels("next draw sees changed source, first result stays intact",&second,80,208,112);

        view_t view={.set_swizzle=1, .swz={NVN_TEXTURE_SWIZZLE_B,NVN_TEXTURE_SWIZZLE_G,NVN_TEXTURE_SWIZZLE_R,NVN_TEXTURE_SWIZZLE_A}};
        nvn_be_reg_tex(3,&a,&view);
        nvn_be_sub_begin(); state_reset(); S.prog=&p; S.tex[1][0]=1; S.tex[1][1]=3;
        clear(&a,32,64,96); clear(&first,0,0,0);
        uint64_t copies_before=g_fb_count;
        targets(&a,&b,&first); draw(&triangle,NULL); finish();
        pixels("same attachment, identity and BGR views",&first,32,96,64);
        CHECK(g_fb_count-copies_before==1,"two views of one subresource issue one copy (got %llu)",
            (unsigned long long)(g_fb_count-copies_before));

        nvn_be_sub_begin(); state_reset(); S.prog=&p; S.tex[1][0]=1; S.tex[1][1]=2;
        clear(&a,0,0,0); clear(&b,0,0,0); clear(&first,0,0,0);
        targets(&a,&b,&first); pending_clear_draw(&triangle); finish();
        pixels("pending ClearColor lands before feedback snapshot",&first,48,176,80);

        // Distinct NVN objects over the same pool bytes share host storage.
        // Pending clears must be visible through those aliases before sampling.
        tex_t alias_a,alias_b;
        make_texture(&alias_a,pool,0); make_texture(&alias_b,pool,65536);
        REQUIRE(alias_a.serial!=a.serial && alias_b.serial!=b.serial);
        REQUIRE(tex_mtl(&alias_a)==tex_mtl(&a) && tex_mtl(&alias_b)==tex_mtl(&b));
        nvn_be_reg_tex(4,&alias_a,NULL); nvn_be_reg_tex(5,&alias_b,NULL);
        nvn_be_sub_begin(); state_reset(); S.prog=&p; S.tex[1][0]=4; S.tex[1][1]=5;
        clear(&a,0,0,0); clear(&b,0,0,0); clear(&first,0,0,0);
        copies_before=g_fb_count;
        targets(&a,&b,&first); pending_clear_draw(&triangle); finish();
        pixels("different serials sharing attachment storage see pending clears",&first,48,176,80);
        CHECK(g_fb_count-copies_before==2,"storage aliases take two distinct feedback snapshots (got %llu)",
            (unsigned long long)(g_fb_count-copies_before));

        // The default read-only feedback path needs to land pending clears too,
        // but does not need to copy attachments this draw does not write.
        g_fb_all=0;
        nvn_be_sub_begin(); state_reset(); S.prog=&p; S.tex[1][0]=4; S.tex[1][1]=5;
        clear(&a,0,0,0); clear(&b,0,0,0); clear(&first,0,0,0);
        copies_before=g_fb_count; uint64_t hits_before=g_fb_hits;
        targets(&a,&b,&first); pending_clear_draw(&triangle); finish();
        pixels("default feedback path lands clears through storage aliases",&first,48,176,80);
        CHECK(g_fb_hits-hits_before==2,"default path detects both aliased attachments (got %llu)",
            (unsigned long long)(g_fb_hits-hits_before));
        CHECK(g_fb_count==copies_before,"default read-only feedback keeps the no-copy path (got %llu)",
            (unsigned long long)(g_fb_count-copies_before));
        g_fb_all=1;

        // Equal dimensions and format do not make disjoint pool ranges alias.
        tex_t independent_a,independent_b;
        make_texture(&independent_a,pool,262144); make_texture(&independent_b,pool,327680);
        nvn_be_reg_tex(6,&independent_a,NULL); nvn_be_reg_tex(7,&independent_b,NULL);
        nvn_be_sub_begin(); state_reset(); S.prog=&p; S.tex[1][0]=6; S.tex[1][1]=7;
        clear(&independent_a,32,64,96); clear(&independent_b,160,192,224);
        clear(&a,0,0,0); clear(&b,0,0,0); clear(&first,0,0,0);
        copies_before=g_fb_count;
        targets(&a,&b,&first); pending_clear_draw(&triangle); finish();
        pixels("same-shape disjoint storage remains independent",&first,32,160,64);
        CHECK(g_fb_count==copies_before,"disjoint storage takes no feedback snapshots (got %llu)",
            (unsigned long long)(g_fb_count-copies_before));
        unlink(map); unlink(user);
    }
    printf("%s t_nvn_feedback_snapshot: %u checks, %u failures\n",failures?"✗":"✓",checks,failures);
    return failures?1:0;
}
