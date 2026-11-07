`timescale 1ns / 1ps

module systolic_pe #(
    parameter DATA_W = 8,
    parameter ACC_W  = 32
) (
    input  wire                     clk,
    input  wire                     rst,
    input  wire signed [DATA_W-1:0] a_in,
    input  wire                     a_valid_in,
    output reg  signed [DATA_W-1:0] a_out,
    output reg                      a_valid_out,
    input  wire signed [DATA_W-1:0] b_in,
    input  wire                     b_valid_in,
    output reg  signed [DATA_W-1:0] b_out,
    output reg                      b_valid_out,
    input  wire                     drain_in,
    output reg                      drain_out,
    input  wire        [ACC_W-1:0]  res_in,
    input  wire                     res_valid_in,
    output reg         [ACC_W-1:0]  res_out,
    output reg                      res_valid_out
);

    reg signed [2*DATA_W-1:0] prod;
    reg                       prod_valid;
    reg signed [ACC_W-1:0]    acc;
    reg                       drain_d;

    // Reset is registered once in every PE, so the array-wide net only reaches D pins and all
    // routing to the flops' R pins stays inside the PE. direct_reset keeps it on the R pin.
    // Otherwise synthesis can merge it into a LUT in front of the register, which adds logic
    // to paths that must also cross an SLR. KEEP stops synthesis merging these across PEs.
    (* keep = "true", direct_reset = "yes" *) reg rst_local;
    always @(posedge clk) rst_local <= rst;

    // Internal copies of the outgoing registers, loaded on the same cycle. Each PE's output
    // registers then drive only the next PE, so the routes between PEs never carry the
    // multiplier or valid logic. KEEP stops synthesis from merging the copies back together.
    (* keep = "true" *) reg signed [DATA_W-1:0] a_mul;
    (* keep = "true" *) reg signed [DATA_W-1:0] b_mul;
    (* keep = "true" *) reg                     a_valid_mul;
    (* keep = "true" *) reg                     b_valid_mul;

    // Datapath registers have no reset so they map cleanly to DSP or fabric registers.
    always @(posedge clk) begin
        a_out <= a_in;
        b_out <= b_in;
        a_mul <= a_in;
        b_mul <= b_in;
        prod  <= a_mul * b_mul;
        if (drain_d) res_out <= acc;
        else         res_out <= res_in;
    end

    always @(posedge clk) begin
        if (rst_local) begin
            a_valid_out   <= 1'b0;
            b_valid_out   <= 1'b0;
            a_valid_mul   <= 1'b0;
            b_valid_mul   <= 1'b0;
            prod_valid    <= 1'b0;
            drain_d       <= 1'b0;
            drain_out     <= 1'b0;
            res_valid_out <= 1'b0;
            acc           <= {ACC_W{1'b0}};
        end else begin
            a_valid_out   <= a_valid_in;
            b_valid_out   <= b_valid_in;
            a_valid_mul   <= a_valid_in;
            b_valid_mul   <= b_valid_in;
            prod_valid    <= a_valid_mul & b_valid_mul;
            drain_d       <= drain_in;
            drain_out     <= drain_d;
            res_valid_out <= drain_d | res_valid_in;
            if (drain_d)         acc <= {ACC_W{1'b0}};
            else if (prod_valid) acc <= acc + prod;
        end
    end

endmodule

module systolic64 #(
    parameter ROWS   = 64,
    parameter COLS   = 64,
    parameter DATA_W = 8,
    parameter ACC_W  = 32
) (
    input  wire                    clk,
    input  wire                    rst,
    input  wire [ROWS*DATA_W-1:0]  a_in,
    input  wire [ROWS-1:0]         a_valid_in,
    input  wire [COLS*DATA_W-1:0]  b_in,
    input  wire [COLS-1:0]         b_valid_in,
    input  wire [COLS-1:0]         drain_in,
    output wire [COLS*ACC_W-1:0]   res_out,
    output wire [COLS-1:0]         res_valid_out
);

    wire [DATA_W-1:0] a_w       [0:ROWS-1][0:COLS];
    wire              a_valid_w [0:ROWS-1][0:COLS];
    wire [DATA_W-1:0] b_w       [0:ROWS][0:COLS-1];
    wire              b_valid_w [0:ROWS][0:COLS-1];
    wire              drain_w   [0:ROWS][0:COLS-1];
    wire [ACC_W-1:0]  res_w     [0:ROWS][0:COLS-1];
    wire              res_valid_w [0:ROWS][0:COLS-1];

    genvar r, c;
    generate
        for (r = 0; r < ROWS; r = r + 1) begin : g_west
            assign a_w[r][0]       = a_in[r*DATA_W +: DATA_W];
            assign a_valid_w[r][0] = a_valid_in[r];
        end
        for (c = 0; c < COLS; c = c + 1) begin : g_edge
            assign b_w[0][c]         = b_in[c*DATA_W +: DATA_W];
            assign b_valid_w[0][c]   = b_valid_in[c];
            assign drain_w[0][c]     = drain_in[c];
            assign res_w[0][c]       = {ACC_W{1'b0}};
            assign res_valid_w[0][c] = 1'b0;
            assign res_out[c*ACC_W +: ACC_W] = res_w[ROWS][c];
            assign res_valid_out[c]          = res_valid_w[ROWS][c];
        end
        for (r = 0; r < ROWS; r = r + 1) begin : g_row
            for (c = 0; c < COLS; c = c + 1) begin : g_col
                systolic_pe #(
                    .DATA_W(DATA_W),
                    .ACC_W(ACC_W)
                ) u_pe (
                    .clk(clk),
                    .rst(rst),
                    .a_in(a_w[r][c]),
                    .a_valid_in(a_valid_w[r][c]),
                    .a_out(a_w[r][c+1]),
                    .a_valid_out(a_valid_w[r][c+1]),
                    .b_in(b_w[r][c]),
                    .b_valid_in(b_valid_w[r][c]),
                    .b_out(b_w[r+1][c]),
                    .b_valid_out(b_valid_w[r+1][c]),
                    .drain_in(drain_w[r][c]),
                    .drain_out(drain_w[r+1][c]),
                    .res_in(res_w[r][c]),
                    .res_valid_in(res_valid_w[r][c]),
                    .res_out(res_w[r+1][c]),
                    .res_valid_out(res_valid_w[r+1][c])
                );
            end
        end
    endgenerate

endmodule
