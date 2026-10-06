// Based on ALU_HW1_TA_testbench.v; preserve TA stimulus and checks.
// Run-specific design, cycle, vector paths and SDF come from the launcher.
`include "run_config.vh"

// Finer precision avoids rounding the synthesis search period.
`timescale 1ns/1fs
`define DATA_NUM 100

`ifdef FXP_adder
    `define FXP
`elsif FXP_mul
    `define FXP
`elsif FLP_adder
    `define FLP
`elsif FLP_mul
   `define FLP
`elsif FLP_adder_7
    `define PIPE 7
    `define adder
`elsif FLP_adder_4
    `define PIPE 4
    `define adder
`elsif FLP_mul_3
    `define PIPE 3
    `define mul
`elsif FLP_mul_4
    `define PIPE 4
    `define mul
`endif

module testbench();
    integer file_a, file_b, file_ans;
    
    reg CLK = 0;
    reg RST = 0;
    reg  [31:0] data_a [0:`DATA_NUM-1];
    reg  [31:0] data_b [0:`DATA_NUM-1];
    reg  [31:0] input_a, temp_a;
    reg  [31:0] input_b, temp_b;
    reg  [63:0] temp_answer;
    wire check;
    `ifdef FXP_mul
        reg  [63:0] answer;
        wire [63:0] outcome;
    `elsif FXP_adder
        reg  [32:0] answer;
        wire [32:0] outcome;
    `else 
        reg  [31:0] answer;
        wire [31:0] outcome;
    `endif
    real real_a, real_b, real_ans, real_outcome, real_error, total_error;

    `ifdef FXP_adder
        FXP_adder test_module( .a(input_a), .b(input_b), .d(outcome));
    `elsif FXP_mul
        FXP_mul   test_module( .a(input_a), .b(input_b), .d(outcome));
    `elsif FLP_adder
        FLP_adder test_module( .a(input_a), .b(input_b), .d(outcome));
    `elsif FLP_mul
        FLP_mul   test_module( .a(input_a), .b(input_b), .d(outcome));
    `elsif FLP_adder_7
        FLP_adder test_module( .a(input_a), .b(input_b), .d(outcome), .rst(RST), .clk(CLK));
    `elsif FLP_adder_4
        FLP_adder test_module( .a(input_a), .b(input_b), .d(outcome), .rst(RST), .clk(CLK));
    `elsif FLP_mul_3
        FLP_mul test_module( .a(input_a), .b(input_b), .d(outcome), .rst(RST), .clk(CLK));
    `elsif FLP_mul_4
        FLP_mul   test_module( .a(input_a), .b(input_b), .d(outcome), .rst(RST), .clk(CLK));
    `endif

    `ifdef SDF_FILE
        initial begin
            $display("HW1_SDF_ANNOTATION_REQUESTED %s", `SDF_FILE);
            $sdf_annotate(`SDF_FILE, test_module,, "sdf_annotation.log", "MAXIMUM");
        end
    `endif

    initial begin
        `ifdef HW1_FSDB
            $fsdbDumpfile("wave.fsdb");
            $fsdbDumpvars(0, testbench);
            $fsdbDumpMDA();
        `else
            $dumpfile("wave.vcd");
            $dumpvars(0, testbench);
        `endif
    end

    // Real division preserves the half-period when CYCLE is an odd integer.
    always begin #(`CYCLE/2.0) CLK = ~CLK; end

    integer i, flag=0, error=0, garbage;
    integer checked=0, result_file;
    initial 
    begin
        file_a      = $fopen(   `FILE_A  , "r");
        file_b      = $fopen(   `FILE_B  , "r");
        if(!file_a || !file_b) $fatal(1, "Cannot open input vectors");

        for(i=0; i<`DATA_NUM; i=i+1)
        begin
            garbage = $fscanf(file_a, "%X", data_a[i]);
            if(garbage != 1) $fatal(1, "Invalid a.txt vector %0d", i+1);
            garbage = $fscanf(file_b, "%X", data_b[i]);
            if(garbage != 1) $fatal(1, "Invalid b.txt vector %0d", i+1);
        end
    end
    
    initial 
    begin
        $display("-----------------------------------------\n");
        `ifdef FXP_adder
            $display("-               FXP_adder               -\n");
        `elsif FXP_mul
            $display("-                FXP_mul                -\n");
        `elsif FLP_adder
            $display("-               FLP_adder               -\n");
        `elsif FLP_mul
            $display("-                FLP_mul                -\n");
        `elsif FLP_adder_7
            $display("-              FLP_adder_7              -\n");
        `elsif FLP_adder_4
            $display("-              FLP_adder_4              -\n");
        `elsif FLP_mul_3
            $display("-               FLP_mul_3               -\n");
        `elsif FLP_mul_4
            $display("-               FLP_mul_4               -\n");
        `endif
        $display("-----------------------------------------\n");

        CLK = 0;
        RST = 1;
        #(`CYCLE*2);
        RST = 0;

        `ifdef FXP
            for(i=0; i<`DATA_NUM; i=i+1)
            begin
                input_a = data_a[i];
                input_b = data_b[i];
                `ifdef FXP_adder
                    answer = $signed({input_a[31], input_a}) + $signed({input_b[31], input_b});
                `elsif FXP_mul
                    answer = $signed(input_a) * $signed(input_b);
                `endif
                #(`CYCLE);
                checked = checked+1;
                if(answer !== outcome)
                begin
                    error = error+1;
                    if(1 || flag==0)
                    begin
                        $display("-----------------------------------------\n");
                        $display("Output error at #%d\n", i+1);
                        $display("The input A is    : %X\n", input_a);
                        $display("The input B is    : %X\n", input_b);
                        $display("The answer is     : %X\n", answer);
                        $display("Your module output: %X\n", outcome);
                        $display("-----------------------------------------\n");
                        flag = 1;
                    end //if flag
                end //if
            end //for
            if(flag==1)//if wrong
            begin
                $display("Total %4d error in %4d testdata.\n", error, i);
                $display("-----------------------------------------\n");
            end//if
            else
            begin//if right
                $display("-----------------------------------------\n");
                $display("All testdata correct!\n");
                $display("-----------------------------------------\n");
            end//else
        `elsif FLP
            total_error = 0;
            for(i=0; i<`DATA_NUM; i=i+1)
            begin
                input_a = data_a[i];
                input_b = data_b[i];
                real_a = $bitstoreal({input_a[31], {3'd0, input_a[30:23]}-11'd127+11'd1023, input_a[22:0], 29'd0});
                real_b = $bitstoreal({input_b[31], {3'd0, input_b[30:23]}-11'd127+11'd1023, input_b[22:0], 29'd0});
                `ifdef FLP_adder
                    real_ans = real_a + real_b;
                `elsif FLP_mul
                    real_ans = real_a * real_b;
                `endif
                temp_answer = $realtobits(real_ans);
                // if(temp_answer[28] && |temp_answer[27:0])
                //     answer = {temp_answer[63], 8'(temp_answer[62:52]-11'd1023+11'd127), temp_answer[51:29]} +1;
                // else if(temp_answer[28] && temp_answer[29])
                //     answer = {temp_answer[63], 8'(temp_answer[62:52]-11'd1023+11'd127), temp_answer[51:29]} +1;
                // else
                answer = {temp_answer[63], 8'(temp_answer[62:52]-11'd1023+11'd127), temp_answer[51:29]};

                // temp_answer[28] is guard bit, round to nearest even
                if( temp_answer[28] == 1'b1 && (((|temp_answer[27:0]) == 1'b1) || (answer%2==1)) )
                    answer = answer + 1'b1;

                // Sample the statistic after settling, just like the score check.
                #(`CYCLE);
                real_outcome = $bitstoreal({outcome[31], {3'd0, outcome[30:23]}-11'd127+11'd1023, outcome[22:0], 29'd0});
                real_error = (real_outcome > real_ans) ? ((real_outcome - real_ans) / real_ans) : ((real_ans - real_outcome) / real_ans);
                total_error = real_error*(1/(i+1.0)) + total_error*(i/(i+1.0));
                checked = checked+1;
                if(!(answer===outcome))
                begin
                    error = error+1;
                    if(1||flag==0)
                    begin
                        $display("-----------------------------------------\n");
                        $display("Output incorrect at #%d\n", i+1);
                        $display("The input A is    : %X\n", input_a);
                        $display("The input B is    : %X\n", input_b);
                        $display("The answer is     : %X\n", answer);
                        $display("Your module output: %X\n", outcome);
                        $display("?: %d\n", answer-outcome);
                        $display("-----------------------------------------\n");
                        flag = 1;
                    end //if flag
                end //if
            end //for
            $display("-----------------------------------------\n");
            $display("Score: %3d\n", 100-error);
            $display("Avg error: %e\n", total_error);
            $display("-----------------------------------------\n");
        `elsif PIPE
            total_error = 0;
            for(i=0; i<`DATA_NUM+`PIPE-2; i=i+1)
            begin
                if(i<`DATA_NUM)
                begin
                    input_a = data_a[i];
                    input_b = data_b[i];
                end
                #(`CYCLE);
                if(i>=(`PIPE-2))
                begin
                    temp_a = data_a[i+2-`PIPE];
                    temp_b = data_b[i+2-`PIPE];
                    real_a = $bitstoreal({temp_a[31], {3'd0, temp_a[30:23]}-11'd127+11'd1023, temp_a[22:0], 29'd0});
                    real_b = $bitstoreal({temp_b[31], {3'd0, temp_b[30:23]}-11'd127+11'd1023, temp_b[22:0], 29'd0});
                    `ifdef adder
                        real_ans = real_a + real_b;
                    `elsif mul
                        real_ans = real_a * real_b;
                    `endif
                    temp_answer = $realtobits(real_ans);
                    answer = {temp_answer[63], 8'(temp_answer[62:52]-11'd1023+11'd127), temp_answer[51:29]};
                    real_outcome = $bitstoreal({outcome[31], {3'd0, outcome[30:23]}-11'd127+11'd1023, outcome[22:0], 29'd0});
                    real_error = (real_outcome > real_ans) ? ((real_outcome - real_ans) / real_ans) : ((real_ans - real_outcome) / real_ans);
                    total_error = real_error*(1/(i-`PIPE+3.0)) + total_error*((i-`PIPE+2)/(i-`PIPE+3.0));
                    // $display("Your module output: %X\n", outcome);
                    checked = checked+1;
                    if(!((answer===(outcome-1)) || (answer===outcome) || (answer ===(outcome+1)) ))
                    begin
                        error = error+1;
                        if(1||flag==0)
                        begin
                            $display("-----------------------------------------\n");
                            $display("Output incorrect at #%d\n", i+3-`PIPE);
                            $display("The input A is    : %X\n", temp_a);
                            $display("The input B is    : %X\n", temp_b);
                            $display("The answer is     : %X\n", answer);
                            $display("Your module output: %X\n", outcome);
                            $display("?: %d\n", answer-outcome);
                            $display("-----------------------------------------\n");
                            flag = 1;
                        end //if flag
                    end //if error
                end //if pipe
            end //for
            $display("-----------------------------------------\n");
            $display("Score: %3d\n", 100-error);
            $display("Avg error: %e\n", total_error);
            $display("-----------------------------------------\n");
        `endif

        $fclose(file_a  );
        $fclose(file_b  );
        // Completion record lets the launcher distinguish a pass from an early exit.
        result_file = $fopen("tb_result.json", "w");
        if(!result_file) $fatal(1, "Cannot write completion record");

        if(error == 0 && checked == `DATA_NUM) begin
            $fwrite(result_file, "{\"status\":\"passed\",\"checked\":%0d,\"errors\":0}\n", checked);
        end else begin
            $fwrite(result_file, "{\"status\":\"failed\",\"checked\":%0d,\"errors\":%0d}\n", checked, error);
        end

        $fclose(result_file);
        if(error != 0 || checked != `DATA_NUM)
            $fatal(1, "HW1_TB_FAILED checked=%0d errors=%0d", checked, error);

        $finish;
    end //initial
endmodule //testbench
