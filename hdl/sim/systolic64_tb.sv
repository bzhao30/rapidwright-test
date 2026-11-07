`timescale 1ns / 1ps

interface systolic_if #(
    parameter ROWS = 64, parameter COLS = 64, parameter DATA_W = 8, parameter ACC_W = 32
) (input logic clk);
    logic                   rst;
    logic [ROWS*DATA_W-1:0] a_in;
    logic [ROWS-1:0]        a_valid_in;
    logic [COLS*DATA_W-1:0] b_in;
    logic [COLS-1:0]        b_valid_in;
    logic [COLS-1:0]        drain_in;
    logic [COLS*ACC_W-1:0]  res_out;
    logic [COLS-1:0]        res_valid_out;
endinterface

module systolic64_tb;
    parameter int ROWS      = 64;
    parameter int COLS      = 64;
    parameter int DATA_W    = 8;
    parameter int ACC_W     = 32;
    parameter int NUM_TESTS = 200;
    parameter int DRAIN_SLACK = 16;  // Cycles beyond the minimum legal drain time.

    localparam int MAX_T = 4096;  // Longest cycle frame the monitor records per test.

    typedef virtual systolic_if #(ROWS, COLS, DATA_W, ACC_W) vif_t;
    typedef logic [ACC_W-1:0] word_t;

    typedef enum {
        K_ZERO, K_IDENT_A, K_IDENT_B, K_MAX_POS, K_MAX_NEG, K_MAX_MIXED, K_ONE_HOT,
        K_A_ONLY, K_B_ONLY, K_EMPTY, K_SINGLE_STEP, K_RESET_MID, K_BUBBLES,
        K_RANDOM_EN, K_RANDOM_LEN, K_RANDOM
    } kind_e;

    // One matrix. Steps are normalized time: step s of row r goes out on cycle s + r.
    // A step with en = 0 is a bubble, and its data is garbage the DUT must ignore.
    class transaction;
        int          id;
        kind_e       kind;
        int          steps;
        int          extra_margin;  // Cycles added beyond the minimum legal drain time.
        bit          reset_mid;
        bit [7:0]    a[];           // [s*ROWS + r]
        bit [7:0]    b[];           // [s*COLS + c]
        bit          a_en[];
        bit          b_en[];

        function new(int id, kind_e kind, int steps);
            this.id    = id;
            this.kind  = kind;
            this.steps = steps;
            a    = new[steps * ROWS];
            b    = new[steps * COLS];
            a_en = new[steps];
            b_en = new[steps];
            foreach (a[i])    a[i]    = $urandom;
            foreach (b[i])    b[i]    = $urandom;
            foreach (a_en[i]) a_en[i] = 1'b1;
            foreach (b_en[i]) b_en[i] = 1'b1;
        endfunction
    endclass

    class expected_item;
        word_t data[];  // [r*COLS + c]
    endclass

    class generator;
        mailbox #(transaction) gen2drv;

        function new(mailbox #(transaction) gen2drv);
            this.gen2drv = gen2drv;
        endfunction

        function automatic bit [7:0] max_pos();
            return {1'b0, {7{1'b1}}};
        endfunction

        function automatic transaction make(int id, kind_e kind);
            transaction t;
            int s = (ROWS > COLS) ? ROWS : COLS;
            bit [7:0] most_neg = 8'h80;
            case (kind)
                K_EMPTY:       t = new(id, kind, 0);
                K_SINGLE_STEP: t = new(id, kind, 1);
                K_RANDOM_LEN:  t = new(id, kind, $urandom_range(s, 1));
                default:       t = new(id, kind, s);
            endcase
            t.extra_margin = ($urandom_range(1, 0) == 0) ? 0 : $urandom_range(20, 1);
            case (kind)
                K_ZERO: begin
                    foreach (t.a[i]) t.a[i] = '0;
                    foreach (t.b[i]) t.b[i] = '0;
                end
                K_IDENT_A: foreach (t.a[i]) t.a[i] = ((i / ROWS) == (i % ROWS));
                K_IDENT_B: foreach (t.b[i]) t.b[i] = ((i / COLS) == (i % COLS));
                K_MAX_POS: begin
                    foreach (t.a[i]) t.a[i] = max_pos();
                    foreach (t.b[i]) t.b[i] = max_pos();
                end
                K_MAX_NEG: begin
                    foreach (t.a[i]) t.a[i] = most_neg;
                    foreach (t.b[i]) t.b[i] = most_neg;
                end
                K_MAX_MIXED: begin
                    foreach (t.a[i]) t.a[i] = max_pos();
                    foreach (t.b[i]) t.b[i] = most_neg;
                end
                K_ONE_HOT: begin
                    int hot_a = $urandom_range(t.a.size() - 1);
                    int hot_b = $urandom_range(t.b.size() - 1);
                    foreach (t.a[i]) t.a[i] = (i == hot_a);
                    foreach (t.b[i]) t.b[i] = (i == hot_b);
                end
                K_A_ONLY:    foreach (t.b_en[i]) t.b_en[i] = 1'b0;
                K_B_ONLY:    foreach (t.a_en[i]) t.a_en[i] = 1'b0;
                K_RESET_MID: t.reset_mid = 1'b1;
                K_BUBBLES: begin
                    foreach (t.a_en[i]) begin
                        bit en = $urandom_range(3, 0) != 0;
                        t.a_en[i] = en;
                        t.b_en[i] = en;
                    end
                end
                K_RANDOM_EN: begin
                    foreach (t.a_en[i]) t.a_en[i] = $urandom_range(1, 0);
                    foreach (t.b_en[i]) t.b_en[i] = $urandom_range(1, 0);
                end
                default: ;
            endcase
            return t;
        endfunction

        task run();
            int id = 0;
            kind_e k = k.first();
            // One pass through every directed kind, then random data for the rest.
            forever begin
                if (id >= NUM_TESTS) break;
                gen2drv.put(make(id, k));
                id++;
                if (k == k.last()) break;
                k = k.next();
            end
            while (id < NUM_TESTS) begin
                int pick = $urandom_range(9, 0);
                kind_e rk = (pick < 6) ? K_RANDOM :
                            (pick < 7) ? K_BUBBLES :
                            (pick < 8) ? K_RANDOM_EN :
                            (pick < 9) ? K_RANDOM_LEN : K_SINGLE_STEP;
                gen2drv.put(make(id, rk));
                id++;
            end
        endtask
    endclass

    class driver;
        vif_t vif;
        mailbox #(transaction) gen2drv;
        mailbox #(bit)         done_mbx;

        function new(vif_t vif, mailbox #(transaction) gen2drv, mailbox #(bit) done_mbx);
            this.vif      = vif;
            this.gen2drv  = gen2drv;
            this.done_mbx = done_mbx;
        endfunction

        task reset();
            vif.rst        <= 1'b1;
            vif.a_valid_in <= '0;
            vif.b_valid_in <= '0;
            vif.drain_in   <= '0;
            repeat (5) @(posedge vif.clk);
            vif.rst <= 1'b0;
            @(posedge vif.clk);
        endtask

        // Applies one cycle of the frame, leaving invalid lanes filled with garbage.
        task drive_cycle(transaction t, int cyc, int drain_cyc);
            logic [ROWS*DATA_W-1:0] a_bus;
            logic [COLS*DATA_W-1:0] b_bus;
            logic [ROWS-1:0]        a_v = '0;
            logic [COLS-1:0]        b_v = '0;
            for (int r = 0; r < ROWS; r++) begin
                int s = cyc - r;
                bit live = (s >= 0 && s < t.steps);
                a_bus[r*DATA_W +: DATA_W] = live ? t.a[s*ROWS + r] : $urandom;
                a_v[r] = live && t.a_en[s];
            end
            for (int c = 0; c < COLS; c++) begin
                int s = cyc - c;
                bit live = (s >= 0 && s < t.steps);
                b_bus[c*DATA_W +: DATA_W] = live ? t.b[s*COLS + c] : $urandom;
                b_v[c] = live && t.b_en[s];
            end
            vif.a_in       <= a_bus;
            vif.b_in       <= b_bus;
            vif.a_valid_in <= a_v;
            vif.b_valid_in <= b_v;
            vif.drain_in   <= (cyc == drain_cyc) ? '1 : '0;
            @(posedge vif.clk);
        endtask

        task run();
            forever begin
                transaction t;
                int stream_end, drain_cyc, last;
                bit done;
                gen2drv.get(t);
                stream_end = t.steps + ((ROWS > COLS) ? ROWS : COLS);
                // Earliest legal drain is T + COLS + 1, with T = steps - 1.
                drain_cyc  = t.steps - 1 + COLS + 1 + DRAIN_SLACK + t.extra_margin;
                if (t.reset_mid) begin
                    for (int c = 0; c < stream_end + DRAIN_SLACK + COLS; c++) drive_cycle(t, c, -1);
                    vif.rst <= 1'b1;
                    repeat (2) @(posedge vif.clk);
                    vif.rst <= 1'b0;
                    @(posedge vif.clk);
                    // Nothing but zeros should remain in the array.
                    drive_cycle(t, t.steps + stream_end, t.steps + stream_end);
                    last = 0;
                end else begin
                    last = (stream_end > drain_cyc) ? stream_end : drain_cyc;
                    for (int c = 0; c <= last; c++) drive_cycle(t, c, drain_cyc);
                end
                vif.a_valid_in <= '0;
                vif.b_valid_in <= '0;
                vif.drain_in   <= '0;
                done_mbx.get(done);
                repeat ($urandom_range(8, 1)) @(posedge vif.clk);
            end
        endtask
    endclass

    class monitor;
        vif_t vif;
        mailbox #(expected_item) exp_mbx;
        mailbox #(expected_item) act_mbx;
        mailbox #(bit)           done_mbx;
        int   protocol_errors = 0;

        bit [7:0] a_mem[];
        bit [7:0] b_mem[];
        bit       a_seen[];
        bit       b_seen[];
        int       cyc = 0;
        int       high_t = -1;

        function new(vif_t vif, mailbox #(expected_item) exp_mbx, mailbox #(expected_item) act_mbx,
                     mailbox #(bit) done_mbx);
            this.vif      = vif;
            this.exp_mbx  = exp_mbx;
            this.act_mbx  = act_mbx;
            this.done_mbx = done_mbx;
            a_mem  = new[MAX_T * ROWS];
            b_mem  = new[MAX_T * COLS];
            a_seen = new[MAX_T * ROWS];
            b_seen = new[MAX_T * COLS];
        endfunction

        function automatic void error(string msg);
            protocol_errors++;
            if (protocol_errors <= 10) $error("[monitor] t=%0t %s", $time, msg);
        endfunction

        function automatic void clear_frame();
            for (int t = 0; t <= high_t; t++) begin
                for (int r = 0; r < ROWS; r++) a_seen[t*ROWS + r] = 1'b0;
                for (int c = 0; c < COLS; c++) b_seen[t*COLS + c] = 1'b0;
            end
            cyc    = 0;
            high_t = -1;
        endfunction

        function automatic void record_inputs();
            for (int r = 0; r < ROWS; r++) begin
                if (vif.a_valid_in[r]) begin
                    int t = cyc - r;
                    if (t < 0 || t >= MAX_T) begin error("A sample outside frame"); continue; end
                    a_mem[t*ROWS + r]  = vif.a_in[r*DATA_W +: DATA_W];
                    a_seen[t*ROWS + r] = 1'b1;
                    if (t > high_t) high_t = t;
                end
            end
            for (int c = 0; c < COLS; c++) begin
                if (vif.b_valid_in[c]) begin
                    int t = cyc - c;
                    if (t < 0 || t >= MAX_T) begin error("B sample outside frame"); continue; end
                    b_mem[t*COLS + c]  = vif.b_in[c*DATA_W +: DATA_W];
                    b_seen[t*COLS + c] = 1'b1;
                    if (t > high_t) high_t = t;
                end
            end
        endfunction

        // Reference model: C[r][c] = sum over t of A[t][r] * B[t][c], signed.
        function automatic expected_item model();
            longint sum[] = new[ROWS * COLS];
            expected_item e = new();
            foreach (sum[i]) sum[i] = 0;
            for (int t = 0; t <= high_t; t++) begin
                for (int r = 0; r < ROWS; r++) begin
                    if (!a_seen[t*ROWS + r]) continue;
                    for (int c = 0; c < COLS; c++) begin
                        if (!b_seen[t*COLS + c]) continue;
                        sum[r*COLS + c] += longint'($signed(a_mem[t*ROWS + r])) *
                                           longint'($signed(b_mem[t*COLS + c]));
                    end
                end
            end
            e.data = new[ROWS * COLS];
            foreach (sum[i]) e.data[i] = word_t'(sum[i]);
            return e;
        endfunction

        task run();
            bit           active = 1'b0;  // A drain was seen and readout is not finished.
            int           count[] = new[COLS];  // Words received per column.
            int           finished;
            expected_item act;
            forever begin
                @(posedge vif.clk);
                if (vif.rst !== 1'b0) begin
                    clear_frame();
                    active = 1'b0;
                    continue;
                end
                record_inputs();
                if (^vif.res_valid_out === 1'bx) error("res_valid_out is X");
                if (vif.drain_in !== '0) begin
                    if (vif.drain_in !== '1) error("drain_in not uniform across columns");
                    if (active) error("drain during readout");
                    act = new();
                    act.data = new[ROWS * COLS];
                    foreach (count[c]) count[c] = 0;
                    active = 1'b1;
                end
                for (int c = 0; c < COLS; c++) begin
                    if (vif.res_valid_out[c] !== 1'b1) continue;
                    if (!active || count[c] >= ROWS) begin
                        error($sformatf("unexpected result word on column %0d", c));
                        continue;
                    end
                    act.data[count[c]*COLS + c] = vif.res_out[c*ACC_W +: ACC_W];
                    count[c]++;
                end
                finished = active;
                foreach (count[c]) if (count[c] != ROWS) finished = 0;
                if (finished) begin
                    // Modeled here, not at the drain: rows can still be arriving then when ROWS > COLS.
                    exp_mbx.put(model());
                    act_mbx.put(act);
                    active = 1'b0;
                    clear_frame();
                    done_mbx.put(1'b1);
                end else begin
                    cyc++;
                end
            end
        endtask
    endclass

    class scoreboard;
        mailbox #(expected_item) exp_mbx;
        mailbox #(expected_item) act_mbx;
        int checked = 0;
        int failed  = 0;
        int word_errors = 0;

        function new(mailbox #(expected_item) exp_mbx, mailbox #(expected_item) act_mbx);
            this.exp_mbx = exp_mbx;
            this.act_mbx = act_mbx;
        endfunction

        task run();
            forever begin
                expected_item e, a;
                int bad = 0;
                exp_mbx.get(e);
                act_mbx.get(a);
                foreach (e.data[i]) begin
                    if (a.data[i] !== e.data[i]) begin
                        bad++;
                        if (word_errors + bad <= 10)
                            $error("[scoreboard] test %0d C[%0d][%0d] expected %0d got %0d",
                                   checked, i / COLS, i % COLS,
                                   $signed(e.data[i]), $signed(a.data[i]));
                    end
                end
                word_errors += bad;
                if (bad != 0) failed++;
                checked++;
            end
        endtask
    endclass

    logic clk = 1'b0;
    always #2 clk = ~clk;

    systolic_if #(ROWS, COLS, DATA_W, ACC_W) sif (clk);

    systolic64 #(.ROWS(ROWS), .COLS(COLS), .DATA_W(DATA_W), .ACC_W(ACC_W)) dut (
        .clk(clk),
        .rst(sif.rst),
        .a_in(sif.a_in),
        .a_valid_in(sif.a_valid_in),
        .b_in(sif.b_in),
        .b_valid_in(sif.b_valid_in),
        .drain_in(sif.drain_in),
        .res_out(sif.res_out),
        .res_valid_out(sif.res_valid_out)
    );

    initial begin
        automatic mailbox #(transaction)   gen2drv = new(1);
        automatic mailbox #(expected_item) exp_mbx = new();
        automatic mailbox #(expected_item) act_mbx = new();
        automatic generator  gen = new(gen2drv);
        automatic mailbox #(bit) done_mbx = new();
        automatic driver     drv = new(sif, gen2drv, done_mbx);
        automatic monitor    mon = new(sif, exp_mbx, act_mbx, done_mbx);
        automatic scoreboard scb = new(exp_mbx, act_mbx);

        drv.reset();
        fork
            mon.run();
            scb.run();
            drv.run();
            gen.run();
        join_none

        fork
            wait (scb.checked == NUM_TESTS);
            begin
                // Watchdog: far beyond the longest legal test length.
                repeat (NUM_TESTS * (8*(ROWS + COLS) + 1000)) @(posedge clk);
                $error("Timeout after %0d of %0d tests", scb.checked, NUM_TESTS);
            end
        join_any
        repeat (5) @(posedge clk);

        $display("checked=%0d failed=%0d word_errors=%0d protocol_errors=%0d",
                 scb.checked, scb.failed, scb.word_errors, mon.protocol_errors);
        if (scb.checked == NUM_TESTS && scb.failed == 0 && mon.protocol_errors == 0)
            $display("*** TEST PASSED ***");
        else
            $display("*** TEST FAILED ***");
        $finish;
    end
endmodule
