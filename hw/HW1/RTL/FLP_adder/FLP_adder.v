// Combinational homework adder, following revised slides 14-20.
// Normalized binary32 inputs; signed Q3.25 addition and halfway-up rounding.
// Finite guard precision follows the slides, not exact IEEE-754 rounding.
// Exponents wrap on packing. No subnormal, infinity or NaN handling.
module FLP_adder(
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
    assign e_diff = ea - eb;

    // Two low guard bits and three integer bits: Q3.25, 28 bits total.
    assign ma = {2'b00, 1'b1, fa, 2'b00};
    assign mb = {2'b00, 1'b1, fb, 2'b00};
    assign ma_signed = sa ? -ma : ma;
    assign mb_signed = sb ? -mb : mb;

    // Signed arithmetic alignment, then signed addition (slides 15-16).
    assign mb_aligned = mb_signed >>> e_diff;
    assign md_signed = ma_signed + mb_aligned;

    // Convert to unsigned magnitude BEFORE normalization (slide 17).
    assign sd = md_signed[27];
    assign md = sd ? -md_signed : md_signed;

    // Leading-zero priority encoder (slide 18). A negative shift count is
    // NOT a left shift in Verilog: explicitly right-shift when md >= 2.
    always @* begin
        zero_count = 28;
        found_one = 1'b0;

        for (bit_index = 27; bit_index >= 0; bit_index = bit_index - 1) begin
            if (!found_one && md[bit_index]) begin
                zero_count = 27 - bit_index;
                found_one = 1'b1;
            end
        end

        shift_left = zero_count - 2;

        if (md == 28'd0) begin
            shift_left = 0;
            md_norm = 28'd0;
        end else if (shift_left < 0) begin
            md_norm = md >> 1;
        end else begin
            md_norm = md << shift_left;
        end
    end

    assign ed = ea - shift_left;

    // Round to nearest with halfway magnitudes rounded upward (slide 19).
    // Bit 1 is the guard bit; bit 2 is the retained fraction's LSB.
    assign md_round = md_norm[1] ? md_norm + 28'd4 : md_norm;

    // Preserve the carry before extracting Q1.23; renormalize if it is 2.
    assign md_final = md_round[26] ? md_round[26:3] : md_round[25:2];
    assign ed_renorm = ed + (md_round[26] ? 10'sd1 : 10'sd0);

    // Reapply the bias and pack (slide 20). Exact cancellation returns +0.
    assign ed_final = ed_renorm + 10'sd127;
    assign fd = md_final[22:0];
    assign d = (md == 28'd0) ? 32'd0 : {sd, ed_final[7:0], fd};
endmodule
