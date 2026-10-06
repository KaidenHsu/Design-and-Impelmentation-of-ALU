// Three stages with two internal register banks: unpack; multiply;
// combinational normalize/round/pack, following the TA checker schedule.
// Normalized binary32 operands only. Nearest rounding with halfway magnitudes
// rounded upward; exponent wraps, matching the combinational homework design.
module FLP_mul(
    input clk,
    input rst,
    input [31:0] a,
    input [31:0] b,
    output [31:0] d
);
    reg [23:0] s1_ma, s1_mb;
    reg [7:0] s1_ea, s1_eb;
    reg s1_sd;

    reg [47:0] s2_md_raw;
    reg [8:0] s2_esum;
    reg s2_sd;

    wire [47:0] md_raw;
    wire [8:0] esum_biased, esum;
    wire [23:0] md_norm;
    wire [24:0] md_round;
    wire [22:0] md_renorm;
    wire [8:0] esum_norm, esum_renorm;

    // Stage 2: unsigned Q2.46 product and exponent arithmetic in parallel.
    assign md_raw = s1_ma * s1_mb;
    assign esum_biased = {1'b0, s1_ea} + {1'b0, s1_eb};
    assign esum = esum_biased - 9'd127;

    // Stage 3: store normalized fractional bits with the leading 1 implicit.
    assign md_norm = s2_md_raw[47] ? s2_md_raw[46:23] : s2_md_raw[45:22];
    assign esum_norm = s2_esum + {8'b0, s2_md_raw[47]};

    // Restore the hidden 1 and round using the guard bit; retain the carry.
    assign md_round = {2'b01, md_norm[23:1]} + {24'b0, md_norm[0]};
    assign md_renorm = md_round[24] ? md_round[23:1] : md_round[22:0];
    assign esum_renorm = esum_norm + {8'b0, md_round[24]};

    // Final stage is combinational; pack the exponent modulo 256.
    assign d = {s2_sd, esum_renorm[7:0], md_renorm};

    // Active-high synchronous reset. Each bank uses the previous bank's values.
    always @(posedge clk) begin
        if (rst) begin
            s1_ma <= 24'd0;
            s1_mb <= 24'd0;
            s1_ea <= 8'd0;
            s1_eb <= 8'd0;
            s1_sd <= 1'b0;

            s2_md_raw <= 48'd0;
            s2_esum <= 9'd0;
            s2_sd <= 1'b0;
        end else begin
            // Bank 1: unpack the current operands (65 bits).
            s1_ma <= {1'b1, a[22:0]};
            s1_mb <= {1'b1, b[22:0]};
            s1_ea <= a[30:23];
            s1_eb <= b[30:23];
            s1_sd <= a[31] ^ b[31];

            // Bank 2: product, exponent and sign from the same operands (58 bits).
            s2_md_raw <= md_raw;
            s2_esum <= esum;
            s2_sd <= s1_sd;
        end
    end
endmodule
