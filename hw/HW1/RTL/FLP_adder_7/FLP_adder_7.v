// 7 combinational stages with 6 internal register banks.
// Final output is combinational, matching the TA PIPE-2 checker.
// Same Q3.25 / halfway-up / exponent-wrap arithmetic as FLP_adder.
// Add/sub: a + b; represent subtraction by reversing b[31].
// Normalized operands only; no special-value handling.
module FLP_adder(
    input clk,
    input rst,
    input [31:0] a,
    input [31:0] b,
    output [31:0] d
);
    wire s1, s2;
    wire [7:0] e1, e2;
    wire [22:0] f1, f2;

    wire sa, sb;
    wire [22:0] fa, fb;
    wire signed [9:0] ea, eb;
    wire [7:0] e_diff;

    wire signed [27:0] ma, mb;
    wire signed [27:0] ma_signed, mb_signed, mb_aligned;
    wire signed [27:0] md_signed;
    wire [27:0] md;
    wire sd;

    integer bit_index;
    integer zero_count;
    integer shift_left;
    reg found_one;
    reg [27:0] md_norm;

    wire signed [9:0] ed, ed_renorm, ed_final;
    wire [27:0] md_round;
    wire [23:0] md_final;
    wire [22:0] fd;

    reg signed [27:0] s1_ma_signed;
    reg signed [27:0] s1_mb_signed;
    reg signed [9:0] s1_ea;
    reg [7:0] s1_e_diff;

    reg signed [27:0] s2_ma_signed;
    reg signed [27:0] s2_mb_aligned;
    reg signed [9:0] s2_ea;

    reg signed [27:0] s3_md_signed;
    reg signed [9:0] s3_ea;

    reg [27:0] s4_md;
    reg s4_sd;
    reg signed [9:0] s4_ea;
    reg s4_zero;

    reg [27:0] s5_md_norm;
    reg signed [9:0] s5_ed;
    reg s5_sd;
    reg s5_zero;

    reg [23:0] s6_md_final;
    reg signed [9:0] s6_ed_renorm;
    reg s6_sd;
    reg s6_zero;

    // Unpack and swap all fields together so that ea >= eb (slide 14).
    assign s1 = a[31];
    assign s2 = b[31];
    assign e1 = a[30:23];
    assign e2 = b[30:23];
    assign f1 = a[22:0];
    assign f2 = b[22:0];

    assign sa = (e1 >= e2) ? s1 : s2;
    assign sb = (e1 >= e2) ? s2 : s1;
    assign fa = (e1 >= e2) ? f1 : f2;
    assign fb = (e1 >= e2) ? f2 : f1;
    assign ea = $signed({2'b00, (e1 >= e2) ? e1 : e2}) - 10'sd127;
    assign eb = $signed({2'b00, (e1 >= e2) ? e2 : e1}) - 10'sd127;
    assign e_diff = (e1 >= e2) ? e1 - e2 : e2 - e1;

    // Two low guard bits and three integer bits: Q3.25, 28 bits total.
    assign ma = $signed({2'b00, 1'b1, fa, 2'b00});
    assign mb = $signed({2'b00, 1'b1, fb, 2'b00});
    assign ma_signed = sa ? -ma : ma;
    assign mb_signed = sb ? -mb : mb;

    // Signed arithmetic alignment, then signed addition (slides 15-16).
    assign mb_aligned = s1_mb_signed >>> s1_e_diff;

    // Signed fixed-point addition.
    assign md_signed = s2_ma_signed + s2_mb_aligned;

    // Convert to unsigned magnitude BEFORE normalization (slide 17).
    assign sd = s3_md_signed[27];
    assign md = $unsigned(sd ? -s3_md_signed : s3_md_signed);

    // Leading-zero priority encoder (slide 18). A negative shift count is
    // NOT a left shift in Verilog: explicitly right-shift when s4_md >= 2.
    always @* begin
        zero_count = 28;
        found_one = 1'b0;

        for (bit_index = 27; bit_index >= 0; bit_index = bit_index - 1) begin
            if (!found_one && s4_md[bit_index]) begin
                zero_count = 27 - bit_index;
                found_one = 1'b1;
            end
        end

        shift_left = zero_count - 2;

        if (s4_md == 28'd0) begin
            shift_left = 0;
            md_norm = 28'd0;
        end else if (shift_left < 0) begin
            md_norm = s4_md >> 1;
        end else begin
            md_norm = s4_md << shift_left;
        end
    end

    assign ed = s4_ea - $signed(shift_left[9:0]);

    // Round to nearest with halfway magnitudes rounded upward (slide 19).
    // Bit 1 is the guard bit; bit 2 is the retained fraction's LSB.
    assign md_round = s5_md_norm[1] ? s5_md_norm + 28'd4 : s5_md_norm;

    // Preserve the carry before extracting Q1.23; renormalize if it is 2.
    assign md_final = md_round[26] ? md_round[26:3] : md_round[25:2];
    assign ed_renorm = s5_ed + (md_round[26] ? 10'sd1 : 10'sd0);

    // Reapply the bias and pack (slide 20). Exact cancellation returns +0.
    assign ed_final = s6_ed_renorm + 10'sd127;
    assign fd = s6_md_final[22:0];
    assign d = s6_zero ? 32'd0 : {s6_sd, ed_final[7:0], fd};

    // Bank 1: unpack and convert operands. Active-high synchronous reset.
    always @(posedge clk) begin
        if (rst) begin
            s1_ma_signed <= 28'd0;
            s1_mb_signed <= 28'd0;
            s1_ea <= 10'd0;
            s1_e_diff <= 8'd0;
        end else begin
            s1_ma_signed <= ma_signed;
            s1_mb_signed <= mb_signed;
            s1_ea <= ea;
            s1_e_diff <= e_diff;
        end
    end

    // Bank 2: align smaller operand. Active-high synchronous reset.
    always @(posedge clk) begin
        if (rst) begin
            s2_ma_signed <= 28'd0;
            s2_mb_aligned <= 28'd0;
            s2_ea <= 10'd0;
        end else begin
            s2_ma_signed <= s1_ma_signed;
            s2_mb_aligned <= mb_aligned;
            s2_ea <= s1_ea;
        end
    end

    // Bank 3: signed addition. Active-high synchronous reset.
    always @(posedge clk) begin
        if (rst) begin
            s3_md_signed <= 28'd0;
            s3_ea <= 10'd0;
        end else begin
            s3_md_signed <= md_signed;
            s3_ea <= s2_ea;
        end
    end

    // Bank 4: convert result to sign-magnitude. Active-high synchronous reset.
    always @(posedge clk) begin
        if (rst) begin
            s4_md <= 28'd0;
            s4_sd <= 1'd0;
            s4_ea <= 10'd0;
            s4_zero <= 1'd1;
        end else begin
            s4_md <= md;
            s4_sd <= sd;
            s4_ea <= s3_ea;
            s4_zero <= md == 28'd0;
        end
    end

    // Bank 5: normalize and adjust exponent. Active-high synchronous reset.
    always @(posedge clk) begin
        if (rst) begin
            s5_md_norm <= 28'd0;
            s5_ed <= 10'd0;
            s5_sd <= 1'd0;
            s5_zero <= 1'd1;
        end else begin
            s5_md_norm <= md_norm;
            s5_ed <= ed;
            s5_sd <= s4_sd;
            s5_zero <= s4_zero;
        end
    end

    // Bank 6: round and renormalize. Active-high synchronous reset.
    always @(posedge clk) begin
        if (rst) begin
            s6_md_final <= 24'd0;
            s6_ed_renorm <= 10'd0;
            s6_sd <= 1'd0;
            s6_zero <= 1'd1;
        end else begin
            s6_md_final <= md_final;
            s6_ed_renorm <= ed_renorm;
            s6_sd <= s5_sd;
            s6_zero <= s5_zero;
        end
    end

endmodule
